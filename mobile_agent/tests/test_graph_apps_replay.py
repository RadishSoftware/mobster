import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.graph import nodes as graph_nodes
from mobile_agent.packs import register as packs
from mobile_agent.tests import test_packs_instagram as igt
from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task


def ctx(extras, events=None):
    return SimpleNamespace(extras=extras, cancelled=lambda: False, emit=(events if events is not None else []).append,
                           runtime=None, run_id="r1", data_dir=Path(tempfile.gettempdir()), origin="workflow",
                           engine="smart", goal="Read", app_bundle=None, device_id=None)


class PackReadTests(unittest.TestCase):
    def test_a_read_gives_rows_by_code_and_taps_nothing_it_shouldnt(self):
        device, events = igt.phone("dm-inbox"), []
        with patch.object(packs, "SLEEP", lambda s: None):
            done = packs.pack_read(ctx({"packRead": {"pack": "instagram", "reader": "dms", "n": 3, "arg": None}},
                                       events), device, None)
        result = done.result
        self.assertEqual((result["status"], result["outcome"], result["costUsd"], result["model"]),
                         ("completed", "done", 0.0, None))
        self.assertEqual(len(result["data"]["rows"]), 3)
        self.assertEqual(result["data"]["rows"][0]["from"], "Sam Lee")
        self.assertEqual(events[0], {"event": "skill_started", "op": "IG_READ"})
        self.assertFalse(graph_nodes.outward(SimpleNamespace(events=events)))

    def test_the_run_field_names_a_pack_and_one_of_its_readers(self):
        self.assertEqual(packs.validate_pack_read({"pack": "instagram", "reader": "dms", "n": 5}, None),
                         {"pack": "instagram", "reader": "dms", "n": 5, "arg": None})
        for bad in ({"pack": "nope", "reader": "dms"}, {"pack": "instagram", "reader": "money"},
                    {"pack": "instagram", "reader": "dms", "n": 99}, {"pack": "instagram", "reader": "dms", "x": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                packs.validate_pack_read(bad, None)
        self.assertIsNone(packs.pack_read(ctx({}), igt.phone("dm-inbox"), None))


class ModeTests(unittest.TestCase):
    def test_replay_only_never_calls_the_model(self):
        graph_nodes.register()
        ended = graph_nodes.replay_only(ctx({"replay": "only"}), None, None)
        self.assertEqual((ended.result["status"], ended.result["model"]), ("completion_not_confirmed", None))
        self.assertIn("set to replay only", ended.result["reason"])
        from mobile_agent.learned.workflows import RUNS
        RUNS.get("r2")["prefix"] = [{"op": "TAP"}, {"op": "TAP"}]
        try:
            again = graph_nodes.replay_only(SimpleNamespace(**{**vars(ctx({"replay": "only"})), "run_id": "r2"}), None,
                                            None)
        finally:
            RUNS.pop("r2")
        self.assertIn("replayed 2 learned steps", again.result["reason"])
        self.assertIsNone(graph_nodes.replay_only(ctx({}), None, None))

    def test_the_model_every_time_skips_learned_and_taught_steps(self):
        from mobile_agent.learned import app_path
        from mobile_agent.learned import workflows as learned
        with patch.object(learned, "taught_fits", side_effect=AssertionError("it looked for a learned path")):
            self.assertIsNone(learned.pre_run(ctx({"replay": "off"}), None, None))
        for mode in ("off", "only"):
            self.assertIsNone(app_path.pre_run(ctx({"replay": mode}), None, None))


class GraphTests(GraphBase):
    def test_an_app_read_feeds_its_rows_to_a_loop(self):
        def script(goal, fields):
            spec = (fields.get("extras") or {}).get("packRead")
            if spec:
                return "completed", {"data": {"rows": [{"from": "Sam"}, {"from": "Kate"}], "columns": [],
                                              "title": "2 newest DMs"}}, 0.0
            return "completed", {"answer": f"Did: {goal}"}, 0.0
        self.phone.script = script
        workflow = self.save_graph(
            [node("read", "app.read", {"app": "instagram", "reader": "dms", "n": 2}),
             node("each", "loop", {"over": "items", "collect": "say"}),
             task("say", "Thank the person in the input", input="{= $item.from }", parent="each")],
            [edge("e1", "start", "read"), edge("e2", "read", "each", "rows", "items"),
             edge("e3", "each", "say", "item")])
        _, record = self.go(workflow)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(self.phone.calls[0].extras["packRead"], {"pack": "instagram", "reader": "dms", "n": 2,
                                                                  "arg": None})
        self.assertEqual([c.extras["workflowInput"] for c in self.phone.calls[1:]], ["Sam", "Kate"])
        self.assertEqual(record["nodes"]["read"]["output"]["title"], "2 newest DMs")

    def test_a_steps_mode_rides_with_its_task(self):
        workflow = self.save_graph([task("a", "Log water", mode="replay"), task("b", "Ask about the weather",
                                                                               mode="agent")],
                                   [edge("e1", "start", "a"), edge("e2", "a", "b")])
        self.go(workflow)
        self.assertEqual([c.extras.get("replay") for c in self.phone.calls], ["only", "off"])

"""The reach replay (bench/write_proof_reach.py) rebuilds a recorded run's contract, its commits included: offline."""

import json
import pathlib
import tempfile
import unittest

from mobile_agent import contract as K
from mobile_agent.bench import write_proof_reach as R

GOAL = "Request a CityRideX from Home to Work in CityRide, then post in TeamChat #ops that I'm late."
ITEMS = ["COMMIT request_money (CityRide): CityRideX ride from Home to Work",
         "COMMIT post (TeamChat): status update in #ops"]


def turn(step, app, *rows, out=None):
    lines = "\n".join(f'e{i + 1} {role} "{label}" @4,{10 + 6 * i},90,5' for i, (role, label) in enumerate(rows))
    return {"step": step, "text": f"Request: {GOAL}\nCurrent app: {app}\n\nScreen elements:\n{lines}",
            "out": out or {}}


def run_folder(actions, turns, evaluation=None):
    folder = pathlib.Path(tempfile.mkdtemp()) / "batch" / "A1" / "033-multi-005"
    folder.mkdir(parents=True)
    events = [{"event": "frontier_contract", "items": ITEMS}] + actions
    (folder / "task.json").write_text(json.dumps({"goal": GOAL, "mobster": {"events": events},
                                                  "evaluation": evaluation or {}}))
    (folder / "prompts.jsonl").write_text("\n".join(json.dumps(t) for t in turns))
    return folder


class RebuildTests(unittest.TestCase):
    def test_a_declared_commit_is_replayed_and_awaits_its_receipt(self):
        folder = run_folder(
            [{"event": "frontier_action", "step": 1, "operation": "TAP", "target_label": "Request CityRideX · $12.80",
              "app_name": "CityRide"}],
            [turn(1, "CityRide", ("StaticText", "Choose a ride"), ("Button", "Request CityRideX · $12.80")),
             turn(2, "CityRide", ("StaticText", "Driver assigned"), ("StaticText", "Driver arriving"))])
        ride = R.rebuild(K, folder).items[0]
        self.assertEqual(ride.executions, 1)
        self.assertEqual(ride.receipts[0]["receipt"], "shown: driver arriving")

    def test_a_send_that_appeared_mid_chain_is_replayed_by_its_label(self):
        folder = run_folder(
            [{"event": "frontier_action", "step": 1, "operation": "TYPE", "target_label": "composer_text_field",
              "app_name": "TeamChat", "text": "Running late"},
             {"event": "frontier_action", "step": 1, "operation": "TAP", "target_label": "Send",
              "app_name": "TeamChat"}],
            [turn(1, "TeamChat", ("StaticText", "#ops"), ("TextField", "composer_text_field")),
             turn(2, "TeamChat", ("StaticText", "#ops"), ("StaticText", "Running late"))])
        rebuilt = R.rebuild(K, folder)
        post = rebuilt.items[1]
        self.assertEqual(post.executions, 1)
        self.assertTrue(post.receipts)
        self.assertEqual(rebuilt.unframed, {post.id})  # taken by its label: the replay has no frame for it

    def test_a_rows_selected_mark_is_read_outside_its_value(self):
        rows = R.elements('Screen elements:\ne1 Button "Like" selected @3,84,7,3\n'
                          'e2 Button "Filter" value="Show selected only" @3,90,7,3')
        self.assertEqual([(e.label, e.selected) for e in rows], [("Like", True), ("Filter", False)])

    def test_a_refused_commit_is_not_replayed(self):
        # The gate refuses a control naming an act no item declares: the replay never counts it as run.
        folder = run_folder(
            [{"event": "frontier_action", "step": 1, "operation": "TAP", "target_label": "Delete ride history",
              "app_name": "CityRide"}],
            [turn(1, "CityRide", ("Button", "Delete ride history")),
             turn(2, "CityRide", ("StaticText", "Driver arriving"))])
        self.assertEqual([i.executions for i in R.rebuild(K, folder).items], [0, 0])

    def test_the_judge_is_quoted_on_the_criteria_that_name_the_items_app(self):
        folder = run_folder([], [turn(1, "CityRide")], {"rubric_results": [
            {"criterion": "Open CityRide and request a CityRideX", "satisfied": True},
            {"criterion": "Post in TeamChat #ops", "satisfied": False}]})
        self.assertEqual(R.judged_on(folder, "CityRide"), [("Open CityRide and request a CityRideX", True)])


if __name__ == "__main__":
    unittest.main()

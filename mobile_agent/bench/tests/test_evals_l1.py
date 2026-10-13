"""L1 frozen sets, their builders, runners and scorers. Offline: model calls are faked; no device or network."""

import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from mobile_agent.bench.evals import build as B
from mobile_agent.bench.evals import l0
from mobile_agent.bench.evals import l1
from mobile_agent.bench.evals import models as M
from mobile_agent.bench.evals import sets as S
from mobile_agent.bench.evals import traces as T
from mobile_agent.bench.tests.test_evals_l0 import HOME, INBOX, MESSAGE, passing_path, turn, write_run

SPLITS = {"splits": {"dev": ["demo-001", "demo-002", "demo-003", "demo-004"], "dev_core": ["demo-001", "demo-002"],
                     "val": ["demo-val"], "test": ["demo-test"]},
          "tasks": {f"demo-00{i}": {"category": "multi_app"} for i in range(1, 5)}}


def contract_turn(step, rows, actions, updates=(), answer=None):
    """A prompt line whose text carries a task contract with a REPORT item, as the agent's prompts do."""
    p = turn(step, rows, actions, answer=answer)
    p["text"] = p["text"].replace("Request: demo\n\n", "Request: demo\n\nApps you may use:\n- Alpha: com.example.alpha\n\n")
    p["text"] = p["text"].replace("\n\nScreen elements:", "\n\nTask contract (code enforces it):\n"
                                  "1. [ ] READ (Alpha): inbox\n2. [ ] REPORT (Alpha): Dana's phone number"
                                  "\n\nScreen elements:")
    p["out"]["checklist_updates"] = list(updates)
    return p


def runs_with_values(root):
    good = [contract_turn(0, HOME, [("TAP", "e2", None)]), contract_turn(1, INBOX, [("TAP", "e3", None)]),
            contract_turn(2, MESSAGE, [("DONE", None, None)], updates=[{"id": 2, "status": "found",
                                                                         "value": "555-0101"}], answer="555-0101")]
    write_run(root, "b1", "A1", "demo-001", good, True)
    write_run(root, "b1", "A2", "demo-001", good, True)
    bad = [contract_turn(0, HOME, [("TAP", "e2", None)]), contract_turn(1, INBOX, [("DONE", None, None)], answer="?")]
    write_run(root, "b1", "B1", "demo-001", bad, False)
    return T.discover([root])


class SetTests(unittest.TestCase):
    def test_halves_alternate_within_strata_and_are_stable(self):
        halves = S.halves_from_splits(SPLITS)
        self.assertEqual(halves, S.halves_from_splits(SPLITS))
        core = [halves[t] for t in ("demo-001", "demo-002")]
        self.assertEqual(sorted(core), ["report", "tune"])
        self.assertNotIn("demo-val", halves)
        self.assertEqual(S.forbidden_tasks(SPLITS), {"demo-val", "demo-test"})

    def test_a_frozen_set_refuses_val_and_test_tasks_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as root:
            halves = {**S.halves_from_splits(SPLITS), "demo-test": "tune"}
            w = S.SetWriter(root, "demo", "v1", halves, S.forbidden_tasks(SPLITS))
            self.assertTrue(w.add({"id": "a", "task": "demo-001"}))
            self.assertFalse(w.add({"id": "b", "task": "demo-test"}))
            manifest = w.write()
            self.assertEqual((manifest["items"], manifest["skipped"]["forbidden"]), (1, 1))
            _, items = S.load(pathlib.Path(root) / "demo" / "v1")
            self.assertEqual([i["id"] for i in items], ["a"])
            with self.assertRaises(FileExistsError):
                S.SetWriter(root, "demo", "v1", halves)
            path = pathlib.Path(root) / "demo" / "v1" / "items.jsonl"
            path.write_text(path.read_text().replace('"a"', '"z"'))
            with self.assertRaises(ValueError):
                S.load(pathlib.Path(root) / "demo" / "v1")


class BuildTests(unittest.TestCase):
    def test_done_items_carry_the_record_and_both_labels(self):
        with tempfile.TemporaryDirectory() as root:
            runs = runs_with_values(root)
            w = S.SetWriter(root, "done", "v1", S.halves_from_splits(SPLITS))
            labels = {("b1", "B1", "001-demo-001"): {"cause": "judge_miss"}}
            items = [B.done_item(r, w, labels) for r in runs]
        done = {i["id"]: i for i in items if i}
        self.assertEqual(len(done), 3)
        bad = done["b1/B1/demo-001"]
        self.assertEqual((bad["label"]["judge_pass"], bad["label"]["adjudicated_pass"]), (False, True))
        self.assertEqual(bad["label"]["missing"], ["c"])
        self.assertIn("REPORT (Alpha)", bad["contract"])
        self.assertEqual([s["step"] for s in bad["steps"]], [0, 1])

    def test_value_items_keep_values_shown_on_their_screen_and_add_negatives(self):
        with tempfile.TemporaryDirectory() as root:
            runs = runs_with_values(root)
            w = S.SetWriter(root, "value", "v1", S.halves_from_splits(SPLITS))

            class Always:
                def random(self):
                    return 0.0

                def choice(self, xs):
                    return xs[0]
            items = [i for r in runs for i in B.value_items(r, w, Always())]
        shown = [i for i in items if i["kind"] == "shown"]
        self.assertTrue(shown and all(i["gold"] == "555-0101" for i in shown))
        self.assertTrue(any(i["kind"] == "not_shown" and i["gold"] is None for i in items))
        self.assertTrue(B.on_screen("P 5g, C 27g", ['e1 StaticText "P 5g  C 27g  F 3g"']))
        self.assertFalse(B.on_screen("555-0199", ['e1 StaticText "555-0101"']))

    def test_golden_items_label_path_done_and_divergence_turns(self):
        with tempfile.TemporaryDirectory() as root:
            write_run(root, "b1", "A1", "demo-001", passing_path(), True)
            write_run(root, "b1", "A2", "demo-001", passing_path(), True)
            write_run(root, "b1", "B1", "demo-001", [turn(0, HOME, [("TAP", "e2", None)]),
                                                     turn(1, INBOX, [("TAP", "e2", None)]),
                                                     turn(2, HOME, [("DONE", None, None)], answer="x")], False)
            for folder in pathlib.Path(root).glob("b1/*/*"):
                for p in folder.glob("prompts.jsonl"):
                    rows = [json.loads(line) for line in p.read_text().splitlines()]
                    for r in rows:
                        r["image"] = f"shot-{r['step']}.jpg"
                        (folder / r["image"]).write_bytes(b"\xff\xd8jpeg")
                    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            runs = T.discover([root])
            w = S.SetWriter(root, "golden", "v1", S.halves_from_splits(SPLITS))
            items = B.golden_items(runs, w)
        kinds = sorted({i["kind"] for i in items})
        self.assertEqual(kinds, ["divergence", "done", "path"])
        div = next(i for i in items if i["kind"] == "divergence")
        self.assertEqual((div["gold"]["label"], div["wrong"]["label"], div["gold"]["target"]),
                         ("Message from Dana", "Home", "e3"))

    def test_contract_items_count_how_often_each_criterion_failed(self):
        with tempfile.TemporaryDirectory() as root:
            runs = runs_with_values(root)
            items = B.contract_items(runs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["apps"], ["Alpha"])
        self.assertEqual(items[0]["criteria"], [{"id": 1, "text": "c", "runs": 3, "failed": 1}])


class ScoreTests(unittest.TestCase):
    def test_done_scores_catches_and_flags_on_both_labels(self):
        items = [{"id": "p", "label": {"judge_pass": True, "adjudicated_pass": True}},
                 {"id": "f", "label": {"judge_pass": False, "adjudicated_pass": False}},
                 {"id": "m", "label": {"judge_pass": False, "adjudicated_pass": True}}]
        rows = [{"id": "p", "accept": False}, {"id": "f", "accept": False}, {"id": "m", "accept": True}]
        score = l1.score_done(rows, items)
        self.assertEqual(score["judge_pass"]["caught"], 1)
        self.assertEqual(score["judge_pass"]["flagged"], 1)
        self.assertAlmostEqual(score["adjudicated_pass"]["accuracy"], .667, places=3)

    def test_value_match_is_lenient_on_formatting_and_scores_abstentions(self):
        self.assertEqual(l1.value_match("P 5g, C 27g, F 3g", "P 5g  C 27g  F 3g"), (True, True))
        self.assertEqual(l1.value_match("Protein: P 5g, C 27g, F 3g", "P 5g, C 27g, F 3g"), (False, True))
        self.assertEqual(l1.value_match("$9.80", "$9.80"), (True, True))
        self.assertEqual(l1.value_match(None, "$9.80"), (False, False))
        items = [{"id": "a", "kind": "shown", "gold": "$9.80"}, {"id": "b", "kind": "not_shown", "gold": None}]
        score = l1.score_value([{"id": "a", "value": "$9.80"}, {"id": "b", "value": None}], items)
        self.assertEqual((score["accuracy"], score["abstain_rate"]), (1.0, 1.0))

    def test_golden_match_by_class_argument_and_unlabeled_id(self):
        text = 'Screen elements:\ne1 Button "Send" @0,0,10,10\ne2 TextField (no label: by position) @0,20,90,5'
        item = {"text": text, "gold": {"op": "SET_TEXT", "label": "", "text": "Seattle", "app": "A", "target": "e2"},
                "alternatives": [["TAP", "send", "", "a", None]], "wrong": None, "step_type": "type", "kind": "path"}
        typed = {"pred": {"op": "TYPE", "label": "e2", "text": "seattle", "app": None}}
        self.assertTrue(l1.golden_match(typed, item)["match"])
        other = {"pred": {"op": "TAP", "label": "Send", "text": None, "app": None, "target": "e1"}}
        self.assertEqual((l1.golden_match(other, item)["match"], l1.golden_match(other, item)["alt"]), (False, True))
        self.assertFalse(l1.golden_match({"pred": None}, item)["alt"])

    def test_contract_recall_counts_content_criteria_only(self):
        items = [{"id": "t", "criteria": [{"id": 1, "failed": 0}, {"id": 2, "failed": 2}, {"id": 3, "failed": 1}]}]
        rows = [{"id": "t", "items": ["READ x", "REPORT y"], "mapping": {"1": [True, []], "2": [False, [2]],
                                                                         "3": [False, []]}}]
        score = l1.score_contract(rows, items)
        self.assertEqual((score["content_criteria"], score["recall"], score["tasks_fully_covered"]), (2, .5, 0.0))
        self.assertEqual(score["unmatched_item_share"], .5)

    def test_a_sample_keeps_the_mix_and_is_reproducible(self):
        items = [{"id": f"{k}{i}", "step_type": k} for k in ("ground", "type") for i in range(10)]
        a, b = l1.sample(items, 6), l1.sample(items, 6)
        self.assertEqual([i["id"] for i in a], [i["id"] for i in b])
        self.assertEqual(sum(i["step_type"] == "ground" for i in a), 3)


class RunnerTests(unittest.TestCase):
    def test_a_run_writes_rows_and_summary_and_charges_the_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            w = S.SetWriter(root, "value", "v1", S.halves_from_splits(SPLITS))
            for task in ("demo-001", "demo-002"):
                w.add({"id": task, "task": task, "kind": "shown", "gold": "555", "request": "r", "question": "q",
                       "app": "Alpha", "lines": ['e1 StaticText "555"'], "image": None})
            w.write()
            ledger = pathlib.Path(root) / "spend.jsonl"
            reply = M.Reply(out={"value": "555"}, usd=.01, seconds=.5, usage={})
            with mock.patch.object(M, "ask", return_value=reply):
                summary, rows = l1.run(pathlib.Path(root) / "value" / "v1", "llm:fake", half="all",
                                       out=pathlib.Path(root) / "out", ledger=ledger, cap=1.0)
            self.assertEqual((summary["items"], summary["accuracy"]), (2, 1.0))
            self.assertTrue((pathlib.Path(root) / "out" / "summary.jsonl").exists())

    def test_the_spend_cap_refuses_a_call(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = pathlib.Path(root) / "spend.jsonl"
            ledger.write_text(json.dumps({"usd": 15.0}) + "\n")
            spend = M.Spend(ledger, 15.0, "test")
            with self.assertRaises(M.OverBudget):
                M.ask("claude-haiku-5-5", "s", ["p"], {"type": "object"}, spend=spend)

    def test_the_marked_screenshot_numbers_each_row_by_its_id(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            buf = io.BytesIO()
            Image.new("RGB", (100, 200), "white").save(buf, "JPEG")
            (pathlib.Path(root) / "assets").mkdir()
            (pathlib.Path(root) / "assets" / "s.jpg").write_bytes(buf.getvalue())
            text = "Request: r\n\nTurns used: 1\n\nScreen elements:\ne1 Button \"Send\" @10,10,50,10\ne4 Image (no label) @0,50,20,20"
            item = {"id": "x", "text": text, "image": "assets/s.jpg"}
            ctx = {"set_dir": pathlib.Path(root), "cache_dir": pathlib.Path(root) / "out"}
            marks = l1.screen_marks(text)
            self.assertEqual([m.id for m in marks], ["1", "4"])
            only, path = l1.observation(item, {"marksonly"}, ctx)
            self.assertNotIn('"Send"', only)
            self.assertTrue(path.exists())
            self.assertEqual(l1.observation(item, {"noimage"}, ctx), (text, None))


class TTFTTests(unittest.TestCase):
    def test_the_ttft_table_separates_hidden_thinking(self):
        run = T.Run(folder="", suite="iosworld", batch="b", arm="A", repeat=1, task="t")
        for i in range(8):
            run.turns.append(T.Turn(step=i, ttft_ms=800 + (2000 if i == 7 else 0), in_tokens=6000, cached_tokens=3500,
                                    out_tokens=100 + (300 if i == 7 else 0), out_chars=300, gen_ms=400,
                                    model_ms=1500, timed=True))
        table = l0.ttft_table([run])
        self.assertEqual(table["cache_missed_turns"], 0)
        self.assertEqual([b["turns"] for b in table["by_hidden_thinking"]], [7, 1])
        self.assertEqual(table["by_hidden_thinking"][1]["ttft_p50_ms"], 2800)


class GroundingTests(unittest.TestCase):
    ROWS = ['e1 NavigationBar "Inbox" @0,5,100,8', 'e2 Button "Back" @2,6,10,4', 'e3 Button "Compose" @85,6,10,4',
            'e4 Cell "Dana, lunch?" @0,20,100,8', 'e5 Cell "Alex, the report" @0,30,100,8',
            'e6 Cell "Sam, see you at noon tomorrow then" @0,40,100,8']

    def test_intents_come_from_the_tree_and_name_one_element(self):
        import random
        rows = B.ground_rows("Screen elements:\n" + "\n".join(self.ROWS))
        made = B.intents(rows, random.Random(1))
        kinds = {k for k, _, _ in made}
        self.assertIn("ordinal", kinds)
        for kind, intent, gold in made:
            self.assertIn(gold, {r["id"] for r in rows})
            if kind == "ordinal":
                order = ["e4", "e5", "e6"]
                self.assertEqual(order.index(gold), ("first", "second", "third").index(intent.split()[1]))

    def test_grounding_scores_exact_ids_by_kind(self):
        items = [{"id": "a", "kind": "label", "gold": "e3"}, {"id": "b", "kind": "ordinal", "gold": "e5"}]
        score = l1.score_grounding([{"id": "a", "target": "e3"}, {"id": "b", "target": None}], items)
        self.assertEqual((score["accuracy"], score["abstained"]), (.5, 1))
        self.assertEqual(score["by_kind"]["label"]["accuracy"], 1.0)



class CommitTests(unittest.TestCase):
    def test_compiled_strings_parse_back_into_items(self):
        items = B.compiled_items(["COMMIT order (Alpha): a ride to work | quote='Request a ride'",
                                  "FORBID delete (): anything | quote=\"don't delete\"", "READ (Alpha): inbox"])
        self.assertEqual([(i["kind"], i["act"], i["app"]) for i in items], [("COMMIT", "order", "Alpha"),
                                                                             ("FORBID", "delete", "")])
        self.assertEqual(items[0]["quote"], "Request a ride")

    def test_the_family_rule_replays_and_scores_by_label(self):
        contracts = [B.compiled_items(["COMMIT send_message (Alpha): reply to Dana | quote='reply to Dana'"])]
        send = {"id": "s", "label": "Send", "role": "Button", "app": "Alpha", "goal": "reply to Dana", "count": 3,
                "requested": True, "required": True, "contracts": contracts}
        buy = {**send, "id": "b", "label": "Buy now", "requested": False, "required": False}
        rows = [{"id": i["id"], **l1.run_commit(i, "family:current", {})} for i in (send, buy)]
        self.assertEqual([r["allow"] for r in rows], [True, False])
        score = l1.score_commit(rows, [send, buy])
        self.assertEqual((score["allowed_when_requested"], score["allowed_when_not_requested"],
                          score["required_now_allowed"], score["decisions"]), (1.0, 0.0, 1, 6))



class FinishTests(unittest.TestCase):
    def test_finishing_is_right_on_true_dones_and_wrong_on_false_ones(self):
        lab = lambda ok: {"adjudicated_pass": ok, "judge_pass": ok}
        items = [{"id": "t1", "label": lab(True)}, {"id": "t2", "label": lab(True)}, {"id": "f1", "label": lab(False)}]
        rows = [{"id": "t1", "pred": {"op": "DONE"}}, {"id": "t2", "pred": {"op": "TAP"}},
                {"id": "f1", "pred": {"op": "SWIPE_UP"}}]
        score = l1.score_finish(rows, items)["adjudicated_pass"]
        self.assertEqual((score["right_finish"], score["wrong_finish"]), (.5, 0.0))
        self.assertAlmostEqual(score["accuracy"], .667, places=3)


if __name__ == "__main__":
    unittest.main()

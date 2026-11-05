"""The offline evals: checkpoints, L0 analytics, L1 sets and scorers, the pairwise judge, the IRT fit."""

import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock
from unittest import mock

from mobile_agent.bench.evals import build as B
from mobile_agent.bench.evals import checkpoint as C
from mobile_agent.bench.evals import l0, l1
from mobile_agent.bench.evals import models as M
from mobile_agent.bench.evals import sets as S
from mobile_agent.bench.evals import traces as T


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.apps = {"alpha": "com.example.alpha", "beta": "com.example.beta"}
        self.containers = {b: root / "sim" / b for b in self.apps.values()}
        for c in self.containers.values():
            (c / "Documents").mkdir(parents=True)
            (c / "Library" / "Preferences").mkdir(parents=True)
        (self.containers["com.example.alpha"] / "Documents" / "state.json").write_text('{"notes": 1}')
        (self.containers["com.example.beta"] / "Library" / "Preferences" / "b.plist").write_text("x")
        self.group = root / "sim" / "group"
        self.group.mkdir()
        (self.group / "shared.db").write_text("g")
        self.root = root
        patches = [mock.patch.object(C, "app_container", lambda udid, bundle: self.containers.get(bundle)),
                   mock.patch.object(C, "group_containers", lambda udid, groups=C.SHARED_GROUPS:
                                     {"group.example": self.group}),
                   mock.patch.object(C, "terminate", lambda udid, bundles: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_restore_puts_back_exactly_what_was_saved(self):
        meta = C.save("U", self.apps, self.root / "cp" / "seed")
        state = self.containers["com.example.alpha"] / "Documents" / "state.json"
        state.write_text('{"notes": 2}')
        (self.containers["com.example.beta"] / "Documents" / "new.txt").write_text("added")
        (self.group / "shared.db").write_text("changed")
        self.assertNotEqual(C.digest("U", self.apps), meta["digest"])
        seconds, ok = C.restore("U", self.apps, self.root / "cp" / "seed")
        self.assertTrue(ok)
        self.assertEqual(state.read_text(), '{"notes": 1}')
        self.assertFalse((self.containers["com.example.beta"] / "Documents" / "new.txt").exists())
        self.assertEqual((self.group / "shared.db").read_text(), "g")

    def test_a_checkpoint_is_never_overwritten(self):
        C.save("U", self.apps, self.root / "cp" / "seed")
        with self.assertRaises(FileExistsError):
            C.save("U", self.apps, self.root / "cp" / "seed")

    def test_a_different_app_manifest_is_refused_before_data_is_cleared(self):
        folder = self.root / "cp" / "seed"
        C.save("U", self.apps, folder)
        state = self.containers["com.example.alpha"] / "Documents" / "state.json"
        state.write_text("current data")
        with mock.patch.object(C, "terminate") as terminate, self.assertRaisesRegex(ValueError, "metadata"):
            C.restore("U", {"alpha": "com.example.alpha"}, folder)
        terminate.assert_not_called()
        self.assertEqual(state.read_text(), "current data")

    def test_a_corrupt_checkpoint_is_refused_before_current_data_is_changed(self):
        for damage in ("changed", "missing"):
            with self.subTest(damage=damage):
                folder = self.root / "cp" / damage
                C.save("U", self.apps, folder)
                state = self.containers["com.example.alpha"] / "Documents" / "state.json"
                state.write_text("current data")
                saved = folder / "apps" / "com.example.alpha" / "Documents" / "state.json"
                if damage == "changed":
                    saved.write_text("damaged snapshot")
                else:
                    saved.unlink()
                with mock.patch.object(C, "terminate") as terminate:
                    _, ok = C.restore("U", self.apps, folder)
                self.assertFalse(ok)
                terminate.assert_not_called()
                self.assertEqual(state.read_text(), "current data")

    def test_only_simulators_with_an_allowed_name_are_touched(self):
        with mock.patch.object(C, "device_name", return_value="iOSWorld Mobster 2"):
            with self.assertRaises(SystemExit):
                C.guard("U")
        with mock.patch.object(C, "device_name", return_value="Eval Lab 1"):
            self.assertEqual(C.guard("U"), "Eval Lab 1")


def screen(*rows, app="Alpha"):
    lines = [f'e{i + 1} {role} "{label}" @0,{i * 5},100,4' for i, (role, label) in enumerate(rows)]
    return f"Request: demo\n\nTurns used: 0 of 50\n\nCurrent app: {app}\n\nScreen elements:\n" + "\n".join(lines)


HOME = (("NavigationBar", "Home"), ("Button", "Inbox"), ("Button", "Settings"))


INBOX = (("NavigationBar", "Inbox"), ("Button", "Home"), ("Button", "Message from Dana"), ("Button", "Close"))


MESSAGE = (("NavigationBar", "Message"), ("StaticText", "Dana: 555-0101"), ("Button", "Close"))


OTHER = (("NavigationBar", "Settings"), ("Button", "Close"))


def turn(step, rows, actions, notes=(), answer=None, app="Alpha"):
    """One prompts.jsonl line: ``actions`` are (operation, target id or None, text or None)."""
    return {"event": "frontier_prompt", "step": step, "text": screen(*rows, app=app),
            "out": {"thought": "t", "notes_add": list(notes), "checklist_updates": [], "answer": answer,
                    "actions": [{"operation": op, "target": target, "text": text, "target_label": None, "app": None}
                                for op, target, text in actions]}}


def events(prompts, changed=None, refused=None):
    """Decision and action events for each prompt (1 s model call, 0.5 s exec, 1 s settle per action)."""
    out = []
    for p in prompts:
        step = p["step"]
        out.append({"event": "frontier_decision", "step": step, "latency_ms": 1000, "prep_ms": 10,
                    "tokens": {"in": 5000, "out": 120, "ttft_ms": 600, "total_ms": 1000}})
        if refused and step in refused:
            out.append({"event": "frontier_refused", "step": step, "label": refused[step]})
            continue
        labels = {f"e{i + 1}": label for i, (_, label) in enumerate(_rows(p["text"]))}
        for index, a in enumerate(p["out"]["actions"]):
            if a["operation"] in ("DONE", "BLOCKED"):
                continue
            out.append({"event": "frontier_action", "step": step, "index": index, "operation": a["operation"],
                        "target_label": labels.get(a["target"] or "", a.get("text") or ""),
                        "changed": (changed or {}).get(step, True), "exec_ms": 500, "settle_ms": 1000,
                        "refresh_ms": 0})
    return out


def _rows(text):
    return [(r, lab) for _, r, lab in T.screen_rows(text)]


def write_run(root, batch, arm, task, prompts, passed, evts=None, category="multi_app"):
    folder = pathlib.Path(root) / batch / arm / f"001-{task}"
    folder.mkdir(parents=True)
    (folder / "prompts.jsonl").write_text("".join(json.dumps(p) + "\n" for p in prompts))
    record = {"task": task, "goal": "demo", "apps": ["alpha"], "category": category, "status": "ok",
              "agent_answer": "a", "mobster": {"status": "completed", "model": "m", "model_calls": len(prompts),
                                               "timing": {"source_reads": len(prompts) * 3, "source_seconds": 3.0},
                                               "events": evts if evts is not None else events(prompts)},
              "evaluation": {"success": passed, "score": 1.0 if passed else 0.5,
                             "rubric_results": [{"criterion": "c", "satisfied": passed}]}}
    (folder / "task.json").write_text(json.dumps(record))
    return folder


def passing_path():
    return [turn(0, HOME, [("TAP", "e2", None)]),
            turn(1, INBOX, [("TAP", "e3", None)]),
            turn(2, MESSAGE, [("DONE", None, None)], notes=["Dana 555-0101"], answer="555-0101")]


class LoaderTests(unittest.TestCase):
    def test_a_task_folder_becomes_turns_with_timing_and_labels(self):
        with tempfile.TemporaryDirectory() as root:
            write_run(root, "b1", "A1", "demo-001", passing_path(), True)
            runs = T.discover([root])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.batch, run.arm, run.repeat, run.task, run.passed), ("b1", "A", 1, "demo-001", True))
        self.assertEqual([t.kind for t in run.turns], ["ground", "ground", "end"])
        self.assertEqual(run.turns[0].actions[0].label, "Inbox")
        self.assertEqual(run.turns[0].title, "Home")
        self.assertAlmostEqual(run.turns[0].seconds, 1.0 + 0.01 + 1.5)
        self.assertEqual(run.turns[0].ttft_ms, 600)
        self.assertTrue(run.turns[2].done_only and run.turns[2].gained)
        self.assertEqual(len(run.actions), 2)

    def test_the_screen_hash_ignores_typed_values_and_app_names_normalize(self):
        a = T.screen_rows('Screen elements:\ne1 TextField "Search" value="dana" @0,0,1,1')
        b = T.screen_rows('Screen elements:\ne1 TextField "Search" value="" @0,0,1,1')
        self.assertEqual(T.screen_hash(a), T.screen_hash(b))
        self.assertEqual(T.app_key("com.example.alpha"), T.app_key("Alpha"))

    def test_long_typed_text_compares_as_one_kind(self):
        long_a = T.Action(op="SET_TEXT", label="body", text="x" * 80, app="Alpha")
        long_b = T.Action(op="SET_TEXT", label="body", text="y" * 80, app="Alpha")
        short = T.Action(op="TYPE", label="search", text="dana", app="Alpha")
        self.assertEqual(long_a.token(), long_b.token())
        self.assertNotEqual(short.token(), T.Action(op="TYPE", label="search", text="alex", app="Alpha").token())

    def test_benchmark_records_and_artifacts_load(self):
        with tempfile.TemporaryDirectory() as root:
            batch = pathlib.Path(root) / "bench1"
            art = batch / "artifacts" / "0_short.demo_agent_x"
            art.mkdir(parents=True)
            prompts = passing_path()
            (art / "prompts.jsonl").write_text("".join(json.dumps(p) + "\n" for p in prompts))
            (art / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events(prompts)))
            (batch / "plan.json").write_text(json.dumps({"wda_url": "http://127.0.0.1:8210"}))
            (batch / "records.jsonl").write_text(json.dumps(
                {"repeat": 0, "task": "short.demo", "agent": "agent+x", "verdict": "pass", "category": "short",
                 "config": {"model": "m"}, "agent_ms": 4000}) + "\n")
            runs = T.discover([root])
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0].suite, runs[0].device, runs[0].passed), ("mobsterbench", "sim", True))
        self.assertEqual(len(runs[0].turns), 3)


class WasteTests(unittest.TestCase):
    def run_of(self, prompts, **kw):
        with tempfile.TemporaryDirectory() as root:
            write_run(root, "b1", "A1", "demo-001", prompts, kw.pop("passed", True),
                      evts=events(prompts, **kw))
            run = T.discover([root])[0]
            return run, {m: (turns, actions, round(seconds, 2)) for m, _, turns, actions, seconds in
                         l0.wasted(run, l0.screen_labels(run))}

    def test_an_opened_screen_left_by_its_close_button_with_nothing_noted_is_a_round_trip(self):
        prompts = [turn(0, HOME, [("TAP", "e3", None)]), turn(1, OTHER, [("TAP", "e2", None)]),
                   turn(2, HOME, [("TAP", "e2", None)]), turn(3, INBOX, [("TAP", "e3", None)]),
                   turn(4, MESSAGE, [("DONE", None, None)], answer="x")]
        _, waste = self.run_of(prompts)
        self.assertEqual(waste["round_trip"][0], 2)

    def test_a_screen_read_before_going_back_is_not_a_round_trip(self):
        prompts = [turn(0, HOME, [("TAP", "e3", None)]), turn(1, OTHER, [("TAP", "e2", None)], notes=["x"]),
                   turn(2, HOME, [("TAP", "e2", None)]), turn(3, INBOX, [("DONE", None, None)], answer="x")]
        _, waste = self.run_of(prompts)
        self.assertNotIn("round_trip", waste)

    def test_refusals_unchanged_actions_and_the_final_done_only_turn(self):
        prompts = [turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("TAP", "e4", None)]),
                   turn(2, INBOX, [("TAP", "e3", None)]), turn(3, MESSAGE, [("DONE", None, None)], answer="x")]
        _, waste = self.run_of(prompts, refused={1: "Send"})
        self.assertEqual(waste["guard_refusal"][0], 1)
        self.assertEqual(waste["done_only_final"][0], 1)
        prompts = [turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("SWIPE_UP", None, None)]),
                   turn(2, INBOX, [("TAP", "e3", None)]), turn(3, MESSAGE, [("DONE", None, None)], answer="x")]
        _, waste = self.run_of(prompts, changed={1: False})
        self.assertEqual(waste["no_change"][:2], (1, 1))

    def test_the_same_action_on_the_same_screen_with_nothing_gained_is_a_repeat(self):
        prompts = [turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("TAP", "e2", None)]),
                   turn(2, HOME, [("TAP", "e2", None)]), turn(3, INBOX, [("TAP", "e3", None)]),
                   turn(4, MESSAGE, [("DONE", None, None)], answer="x")]
        _, waste = self.run_of(prompts)
        self.assertEqual(waste["repeat_action"][0], 1)


class DivergenceTests(unittest.TestCase):
    def runs(self, failing):
        with tempfile.TemporaryDirectory() as root:
            write_run(root, "b1", "A1", "demo-001", passing_path(), True)
            write_run(root, "b1", "A2", "demo-001", passing_path(), True)
            write_run(root, "b1", "B1", "demo-001", failing, False)
            return T.discover([root])

    def test_a_run_that_stops_before_a_milestone_stopped_early(self):
        runs = self.runs([turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("DONE", None, None)], answer="x")])
        (d,) = l0.divergences(runs)
        self.assertEqual(d["kind"], "stopped_early_done")
        self.assertEqual(d["ref_action"][1], "Message from Dana")

    def test_a_different_element_on_the_passing_runs_screen(self):
        runs = self.runs([turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("TAP", "e2", None)]),
                          turn(2, HOME, [("DONE", None, None)], answer="x")])
        (d,) = l0.divergences(runs)
        self.assertEqual(d["kind"], "different_element@same_screen")
        self.assertEqual((d["fail_action"][1], d["ref_action"][1]), ("Home", "Message from Dana"))

    def test_a_run_with_every_milestone_failed_on_its_answer(self):
        runs = self.runs(passing_path())
        (d,) = l0.divergences(runs)
        self.assertEqual(d["kind"], "reached_every_milestone")

    def test_shortest_and_excess(self):
        runs = self.runs(passing_path())
        best = l0.shortest(runs)
        self.assertEqual(best[("iosworld", "demo-001")], (3, 2))
        self.assertEqual(l0.excess(runs[0], best)["turns"], 0)


class ReportTests(unittest.TestCase):
    def test_the_cli_writes_the_ranked_table(self):
        with tempfile.TemporaryDirectory() as root:
            write_run(root, "b1", "A1", "demo-001", passing_path(), True)
            write_run(root, "b1", "B1", "demo-001",
                      [turn(0, HOME, [("TAP", "e2", None)]), turn(1, INBOX, [("DONE", None, None)], answer="x")],
                      False)
            groups = pathlib.Path(root) / "groups.json"
            groups.write_text(json.dumps({"Alpha": "mail"}))
            out = pathlib.Path(root) / "out"
            with unittest.mock.patch("builtins.print"):
                self.assertEqual(l0.main(["--runs", root, "--app-groups", str(groups), "--out", str(out)]), 0)
            data = json.loads((out / "l0.json").read_text())
            text = (out / "l0.md").read_text()
        self.assertIn("Mechanisms ranked by seconds", text)
        self.assertEqual(data["divergence_kinds"], {"stopped_early_done": 1})
        self.assertIn("mail", data["time_sinks"]["by_app_group"])
        shares = sum(r["share"] for r in data["partition"])
        self.assertAlmostEqual(shares, 1.0, places=2)
        self.assertTrue(any(r["mechanism"] == "repeated_reads" for r in data["mechanisms"]))


SPLITS = {"splits": {"dev": ["demo-001", "demo-002", "demo-003", "demo-004"], "dev_core": ["demo-001", "demo-002"],
                     "val": ["demo-val"], "test": ["demo-test"]},
          "tasks": {f"demo-00{i}": {"category": "multi_app"} for i in range(1, 5)}}


def contract_turn(step, rows, actions, updates=(), answer=None):
    """A prompt line whose text carries a task contract with a REPORT item, as the agent's prompts do."""
    p = turn(step, rows, actions, answer=answer)
    p["text"] = p["text"].replace("Request: demo\n\n", "Request: demo\n\nApps you may use:\n- "
                                  "Alpha: com.example.alpha\n\n")
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
            text = ("Request: r\n\nTurns used: 1\n\nScreen elements:\ne1 Button \"Send\" @10,10,50,10\ne4 "
                    "Image (no label) @0,50,20,20")
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


class PairwiseTests(unittest.TestCase):
    def test_a_verdict_counts_only_when_both_orders_agree(self):
        from mobile_agent.bench.evals import pairwise as P
        item = {"text": 'Screen elements:\ne1 Button "Send" @0,0,1,1', "image": None, "id": "x"}
        ctx = {"judge": "j", "spend": None, "set_dir": None, "image": False}
        def fake(answers):
            it = iter(answers)
            return lambda *a, **k: M.Reply(out={"better": next(it), "reason": ""}, usd=.001, seconds=.1)
        cand, rec = {"op": "TAP", "label": "Send", "target": "e1"}, {"op": "DONE", "label": "", "text": "x"}
        with mock.patch.object(M, "ask", fake(["A", "B"])):
            self.assertEqual(P.judge_pair(item, cand, rec, ctx)["verdict"], "candidate")
        with mock.patch.object(M, "ask", fake(["A", "A"])):  # position bias: both say "first"
            out = P.judge_pair(item, cand, rec, ctx)
            self.assertEqual((out["verdict"], out["consistent"]), ("tie", False))
        self.assertEqual(P.describe(cand, {}), 'TAP e1 "Send"')


class IRTTests(unittest.TestCase):
    def test_the_fit_orders_tasks_by_difficulty_and_engines_by_ability(self):
        import random

        from mobile_agent.bench.evals import irt
        rng = random.Random(3)
        truth_theta = {f"p{j}": -1.5 + j * .5 for j in range(7)}
        truth_b = {"easy": -1.5, "mid": 0.0, "hard": 1.5}
        cells = {}
        for p, th in truth_theta.items():
            for t, b in truth_b.items():
                n = 12
                s = sum(rng.random() < irt.sigmoid(1.5 * (th - b)) for _ in range(n))
                cells[(p, t)] = (s, n)
        params = irt.fit(cells, iterations=800)
        self.assertLess(params["b"]["easy"], params["b"]["mid"])
        self.assertLess(params["b"]["mid"], params["b"]["hard"])
        self.assertLess(params["theta"]["p0"], params["theta"]["p6"])
        best = irt.informative(params, ["easy", "mid", "hard"], params["theta"]["p3"], 1)
        self.assertEqual(best, ["mid"])
        self.assertGreater(irt.ability({"mid": (10, 12)}, params), irt.ability({"mid": (2, 12)}, params))


class SyntheticPairTests(unittest.TestCase):
    def test_known_pairs_are_a_random_other_tap_or_an_early_done(self):
        from mobile_agent.bench.evals import pairwise as P
        text = ('Screen elements:\ne1 Button "Send" @0,0,10,10\ne2 Button "Cancel" @0,20,10,10\n'
                'e3 Button "Attach" @0,40,10,10')
        items = [{"id": f"i{n}", "kind": "path", "text": text, "alternatives": [],
                  "gold": {"op": "TAP", "label": "Send", "text": None, "app": None, "target": "e1"}} for n in range(4)]
        pairs = P.synthetic_pairs(items, 4)
        self.assertEqual(len(pairs), 4)
        taps = [bad for _, bad, _ in pairs if bad["op"] == "TAP"]
        self.assertTrue(taps and all(b["target"] in ("e2", "e3") for b in taps))
        self.assertTrue(any(bad["op"] == "DONE" for _, bad, _ in pairs))


class ProxyTests(unittest.TestCase):
    def test_rank_correlation_needs_enough_hypotheses_with_a_paired_ab(self):
        from mobile_agent.bench.evals import proxy
        entries = [{"hypothesis": f"h{i}", "proxies": {"l1:x": x}, "l3": {"pass_delta": y}}
                   for i, (x, y) in enumerate([(.1, .02), (.3, .05), (.2, .04), (.5, .09)])]
        entries.append({"hypothesis": "stopped", "proxies": {"l1:x": .9}})
        report = proxy.validity(entries, minimum=4)
        self.assertEqual((report["with_l3"], report["stopped_early"]), (4, 1))
        self.assertEqual(report["proxies"][0]["spearman"], 1.0)
        self.assertNotIn("spearman", proxy.validity(entries[:3], minimum=4)["proxies"][0])
        self.assertEqual(proxy.ranks([3, 1, 1]), [3.0, 1.5, 1.5])


if __name__ == "__main__":
    unittest.main()

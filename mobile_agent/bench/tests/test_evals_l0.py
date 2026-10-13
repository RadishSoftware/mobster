"""L0 trace analytics and the shared trace loader, on synthetic run folders. Offline: no device, model or network."""

import json
import pathlib
import tempfile
import unittest
import unittest.mock

from mobile_agent.bench.evals import l0
from mobile_agent.bench.evals import traces as T


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


if __name__ == "__main__":
    unittest.main()

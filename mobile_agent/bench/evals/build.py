"""Build the frozen L1 sets from recorded runs. $0: labels come from the runs and the judge, never from a model.

    python -m mobile_agent.bench.evals.build SET --runs DIR... --splits splits.json --root SETS --version v1 \\
        [--labels labels.jsonl ...] [--batches aa1,ac1]

Sets (each item carries ``task`` and its ``half``; see sets.py):

- ``done``: each judged run that ended with DONE, as a completion check would see it at that moment: the request,
  the contract and notes of the final turn, every step's screen rows and what its actions did, the found REPORT
  values, the answer and the final screenshot. Labels: the judge's verdict (iOSWorld's all-criteria rule), the
  unsatisfied criteria (what was missing), and where a failure labeller found a judge miss, the corrected verdict.
- ``value``: a REPORT value the agent marked found on a screen of a run the judge passed, with the screen's rows
  and screenshot, kept only when the value is on that screen's rows; plus negatives: the same question on another
  screen of the run where the value is not shown (gold: none).
- ``golden``: next-action turns (see ``golden`` below). ``grounding``, ``contract`` and ``commit``: see their
  builders.
"""

import argparse
import collections
import json
import pathlib
import hashlib
import random
import re

from . import l0
from . import sets as S
from . import traces as T

CONTRACT_HEAD = "Task contract"
REPORT_LINE = re.compile(r"^(\d+)\. \[([^\]]*)\] (REPORT\b.*|report: .*)$", re.M)
ITEM_LINE = re.compile(r"^(\d+)\. \[([^\]]*)\] (.*)$", re.M)


def section(text, head, ends=("\n\nRecent actions", "\n\nScreen elements:", "\n\nTask contract", "\n\nList read",
                              "\n\nNotes:", "\n\nChecklist:")):
    at = (text or "").find(head)
    if at < 0:
        return ""
    body = text[at + len(head):]
    cut = [body.find(m) for m in ends if body.find(m) >= 0]
    return body[: min(cut)].strip() if cut else body.strip()


def contract_text(text):
    """The final prompt's contract (or older checklist) block, without its instruction line."""
    block = section(text, CONTRACT_HEAD) or section(text, "Checklist:")
    return block.split("\n", 1)[1] if block.startswith("(") and "\n" in block else block


def screen_lines(text):
    at = (text or "").find("Screen elements:\n")
    if at < 0:
        return []
    return [line for line in text[at + len("Screen elements:\n"):].splitlines() if re.match(r"^e\d+ ", line)]


def listing_lines(text):
    """The rows a READ_LIST showed this turn (the prompt's list block), which the screen rows don't hold."""
    at = (text or "").find("List read by READ_LIST")
    if at < 0:
        return []
    body = text[at:].split("\n", 1)[1] if "\n" in text[at:] else ""
    cut = [body.find(m) for m in ("\n\nRecent actions", "\n\nScreen elements:", "\n\nTask contract") if body.find(m) >= 0]
    return [line for line in (body[: min(cut)] if cut else body).splitlines() if line.strip()]


def apps_block(text):
    return section(text, "Apps you may use:\n", ("\n\n",))


def did_lines(events):
    """{step: ["TAP 'Send' -> screen changed", ...]} from a run's events."""
    did = collections.defaultdict(list)
    for e in events or ():
        step = e.get("step")
        if e.get("event") == "frontier_action":
            text = f" text={e['text']!r}" if isinstance(e.get("text"), str) and e.get("text") else ""
            did[step].append(f"{e.get('operation')} {str(e.get('target_label') or '')[:80]!r}{text} -> "
                             + ("screen changed" if e.get("changed") else "NO visible change"))
        elif e.get("event") == "frontier_refused":
            did[step].append(f"refused by Mobster: {str(e.get('label'))[:80]}")
        elif e.get("event") == "frontier_failed":
            did[step].append(f"{e.get('operation')} failed")
    return did


def _prompts(folder):
    return T.read_jsonl(pathlib.Path(folder) / "prompts.jsonl")


def _record(folder):
    return json.loads((pathlib.Path(folder) / "task.json").read_text(encoding="utf-8"))


def _is_done(out):
    return any((a.get("operation") or a.get("op")) == "DONE" for a in (out or {}).get("actions") or ())


def label_index(paths):
    """{(batch, run folder, task folder): label row} from failure-labeller files."""
    out = {}
    for path in paths or ():
        for row in T.read_jsonl(path):
            parts = pathlib.Path(row.get("folder") or "").parts
            if len(parts) >= 3:
                out[(parts[-3], parts[-2], parts[-1])] = row
    return out


# -- done ------------------------------------------------------------------------------------------------------

def done_item(run, writer, labels):
    folder = pathlib.Path(run.folder)
    prompts = _prompts(folder)
    done = [p for p in prompts if _is_done(p.get("out"))]
    if not done or not run.judged or run.status != "completed":
        return None
    final = done[-1]
    record = _record(folder)
    did = did_lines((record.get("mobster") or {}).get("events"))
    steps = []
    for p in prompts:
        if p.get("step") is None or p["step"] > final["step"]:
            continue
        text = p.get("text") or ""
        rows = T.screen_rows(text)
        steps.append({"step": p["step"], "app": section(text, "Current app: ", ("\n\n",)),
                      "title": T.screen_title(rows), "lines": screen_lines(text), "did": did.get(p["step"], []),
                      "listing": listing_lines(text)})
    text = final.get("text") or ""
    contract = contract_text(text)
    reports = [[m.group(3).strip(), m.group(2)[len("found: "):].strip()] for m in REPORT_LINE.finditer(contract)
               if m.group(2).startswith("found: ")]
    lab = labels.get((folder.parent.parent.name, folder.parent.name, folder.name)) or {}
    missing = [c["criterion"] for c in run.criteria if not c.get("satisfied")]
    return {"id": run.id, "task": run.task, "category": run.category, "batch": run.batch, "arm": run.arm,
            "run": run.repeat, "build": run.build, "model": run.model, "request": run.goal,
            "apps": apps_block(text), "contract": contract,
            "notes": [line[2:] for line in section(text, "Notes:\n").splitlines() if line.startswith("- ")],
            "answer": (final.get("out") or {}).get("answer") or run.answer, "steps": steps, "reports": reports,
            "done_step": final["step"], "turns": len(prompts),
            "final_image": writer.asset(folder / final["image"]) if final.get("image") else None,
            "label": {"judge_pass": bool(run.passed), "rubric": run.rubric, "missing": missing,
                      "criteria": run.criteria, "cause": lab.get("cause"),
                      "adjudicated_pass": bool(run.passed) or lab.get("cause") == "judge_miss"}}


# -- value -----------------------------------------------------------------------------------------------------

def _plain(text):
    return re.sub(r"[^a-z0-9.$%:/@+-]+", " ", str(text or "").casefold()).strip()


def value_parts(value):
    """The comparable pieces of a value: "P 5g, C 27g, F 3g" -> ["p 5g", "c 27g", "f 3g"]."""
    parts = [_plain(p) for p in re.split(r"[;,\n]| and ", str(value or "")) if _plain(p)]
    return parts or ([_plain(value)] if _plain(value) else [])


def on_screen(value, lines):
    hay = _plain(" ".join(lines))
    parts = value_parts(value)
    return bool(parts) and all(p in hay for p in parts)


def value_items(run, writer, rng):
    """Positive items (a found REPORT value on its screen) and negatives, for one passing run."""
    if not run.passed:
        return []
    folder = pathlib.Path(run.folder)
    prompts = _prompts(folder)
    out, by_step = [], {p.get("step"): p for p in prompts}
    for p in prompts:
        text = p.get("text") or ""
        contract = contract_text(text)
        items = {int(m.group(1)): m.group(3).strip() for m in ITEM_LINE.finditer(contract)}
        for c in (p.get("out") or {}).get("checklist_updates") or ():
            if not isinstance(c, dict) or c.get("status") != "found" or not c.get("value"):
                continue
            question = items.get(c.get("id"))
            lines = screen_lines(text)
            if not question or not re.match(r"^(REPORT\b|report:)", question) or not lines \
                    or not on_screen(c["value"], lines):
                continue
            base = {"task": run.task, "category": run.category, "request": run.goal, "question": question,
                    "app": section(text, "Current app: ", ("\n\n",)), "source_run": run.id}
            out.append({**base, "id": f"{run.id}/s{p['step']}/r{c['id']}", "step": p["step"], "lines": lines,
                        "screen": T.screen_hash(T.screen_rows(text)),
                        "image": writer.asset(folder / p["image"]) if p.get("image") else None,
                        "gold": c["value"], "kind": "shown"})
            others = [q for s, q in by_step.items() if s != p["step"] and screen_lines(q.get("text") or "")
                      and not on_screen(c["value"], screen_lines(q.get("text") or ""))
                      and section(q.get("text") or "", "Current app: ", ("\n\n",)) == base["app"]]
            if others and rng.random() < .35:
                q = rng.choice(others)
                qtext = q.get("text") or ""
                out.append({**base, "id": f"{run.id}/s{q['step']}/r{c['id']}/neg", "step": q["step"],
                            "lines": screen_lines(qtext), "screen": T.screen_hash(T.screen_rows(qtext)),
                            "image": writer.asset(folder / q["image"]) if q.get("image") else None,
                            "gold": None, "kind": "not_shown"})
    return out


# -- golden turns ------------------------------------------------------------------------------------------------

def _gold(action, target=None):
    return {"op": action.op, "label": action.label, "text": action.text, "app": action.app, "target": target}


def _first_target(prompt):
    acts = ((prompt or {}).get("out") or {}).get("actions") or []
    return acts[0].get("target") if acts and re.fullmatch(r"e\d+", str(acts[0].get("target") or "")) else None


def golden_items(runs, writer, per_task=24, seed=20261010):
    """Next-action turns. ``path``: a turn of a passing run that L0 found no waste in; its alternatives are the first
    actions any passing run of the task took on the same screen (no waste either). ``divergence``: the failing run's
    turn where it first missed a milestone on a screen a passing run was on: gold is the passing run's action there,
    ``wrong`` the failing run's. ``done``: a passing run's final DONE. ``premature_done``: a failing run's DONE
    while a passing run went on from the same screen."""
    rng = random.Random(seed)
    passing = collections.defaultdict(list)
    for r in runs:
        if r.passed and r.turns:
            passing[(r.suite, r.task)].append(r)
    wasted = {}  # turns a better run would not take (a final DONE-only turn and a chainable turn are still right)
    for r in runs:
        wasted[r.id] = {step for mech, step, *_ in l0.wasted(r, l0.screen_labels(r))
                        if mech not in ("done_only_final", "chainable_turn")}
    alts = collections.defaultdict(set)  # (task, screen) -> {(op, label, text, app, target)}
    prompt_cache = {}

    def prompt(run, step):
        if run.id not in prompt_cache:
            prompt_cache[run.id] = {p.get("step"): p for p in _prompts(run.folder)}
        return prompt_cache[run.id].get(step)

    for (suite, task), rs in passing.items():
        for r in rs:
            for t in r.turns:
                if t.step in wasted[r.id] or not t.screen:
                    continue
                first = (t.actions or t.planned or [None])[0]
                if first is not None:
                    alts[(task, t.screen)].add((first.op, T.norm(first.label), T.norm(first.text), T.app_key(first.app),
                                                _first_target(prompt(r, t.step)) if not first.label else None))

    def item(run, turn, kind, gold, wrong=None):
        p = prompt(run, turn.step)
        if not p or not p.get("image"):
            return None
        image = writer.asset(pathlib.Path(run.folder) / p["image"])
        if image is None:
            return None
        return {"id": f"{run.id}/s{turn.step}", "task": run.task, "category": run.category, "kind": kind,
                "source_run": run.id, "step": turn.step, "step_type": T.step_type(gold["op"]), "text": p["text"],
                "operations": p.get("operations"), "image": image, "gold": gold, "wrong": wrong,
                "alternatives": sorted((list(a) for a in alts.get((run.task, turn.screen), ())), key=str),
                "screen": turn.screen, "model": run.model, "build": run.build}

    out = []
    for (suite, task), rs in sorted(passing.items()):
        cands = []
        for r in rs:
            for t in r.turns:
                if not t.has_prompt or t.step in wasted[r.id]:
                    continue
                first = (t.actions or t.planned or [None])[0]
                if first is None:
                    continue
                kind = "done" if first.op in T.END_OPS else "path"
                cands.append((r, t, kind, _gold(first, _first_target(prompt(r, t.step)))))
        rng.shuffle(cands)
        dones = [c for c in cands if c[2] == "done"][:2]
        path = [c for c in cands if c[2] == "path"]
        # spread over step types: take round-robin by type
        by_type = collections.defaultdict(list)
        for c in path:
            by_type[T.step_type(c[3]["op"])].append(c)
        picked = []
        while len(picked) < per_task and any(by_type.values()):
            for k in sorted(by_type):
                if by_type[k] and len(picked) < per_task:
                    picked.append(by_type[k].pop())
        for r, t, kind, gold in picked + dones:
            it = item(r, t, kind, gold)
            if it:
                out.append(it)
    for d in l0.divergences(runs):
        if not d["same_screen"] and not d["kind"].startswith("stopped_early_done"):
            continue
        fail = next(r for r in runs if r.id == d["run"])
        ref = next(r for r in runs if r.id == d["ref"])
        f_turn = next((t for t in fail.turns if t.step == d["step"]), None)
        r_turn = next((t for t in ref.turns if t.step == d["ref_step"]), None)
        if f_turn is None or r_turn is None:
            continue
        r_action = next((a for a in (r_turn.actions or r_turn.planned) if a.label == d["ref_action"][1]
                         and a.op == d["ref_action"][0]), None)
        f_action = (f_turn.actions or f_turn.planned or [None])[0]
        if r_action is None or f_action is None:
            continue
        kind = "premature_done" if d["kind"].startswith("stopped_early_done") else "divergence"
        if kind == "premature_done" and f_turn.screen != r_turn.screen:
            continue
        it = item(fail, f_turn, kind, _gold(r_action, _first_target(prompt(ref, r_turn.step))),
                  wrong=_gold(f_action, _first_target(prompt(fail, f_turn.step))))
        if it:
            out.append(it)
    return out


# -- grounding ------------------------------------------------------------------------------------------------------

GROUND_ROW = re.compile(r'^(e\d+) (\S+) (?:"((?:[^"\\]|\\.)*)"|\(no label[^)]*\))(.*?)@(\d+),(\d+),(\d+),(\d+)\s*$', re.M)
ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")
TAPPABLE = frozenset({"Button", "Cell", "Link", "TextField", "SearchField", "Switch", "Tab", "MenuItem", "Image"})


def ground_rows(text):
    at = (text or "").find("Screen elements:")
    out = []
    for m in GROUND_ROW.finditer(text[at:] if at >= 0 else ""):
        eid, role, label, _, x, y, w, h = m.groups()
        out.append({"id": eid, "role": role, "label": (label or "").replace('\\"', '"'),
                    "rect": (int(x), int(y), int(w), int(h))})
    return out


def intents(rows, rng):
    """(kind, intent, gold id) generated from the tree itself: an exact label, an ordinal in a list, the element
    beside a labelled one, a long label's first words. Each intent names exactly one element."""
    out = []
    labels = collections.Counter(r["label"] for r in rows if r["label"])
    taps = [r for r in rows if r["role"] in TAPPABLE and r["label"] and labels[r["label"]] == 1
            and 0 < r["rect"][2] < 100 or (r["role"] in TAPPABLE and r["label"] and labels[r["label"]] == 1)]
    if taps:
        r = rng.choice(taps)
        out.append(("label", f'the "{r["label"]}" {r["role"].lower()}', r["id"]))
    # a list: three or more rows of one role with the same x and width, stacked
    lists = collections.defaultdict(list)
    for r in rows:
        if r["role"] in ("Button", "Cell") and r["rect"][2] >= 60:
            lists[(r["role"], r["rect"][0], r["rect"][2])].append(r)
    for (role, _, _), members in lists.items():
        members = sorted(members, key=lambda r: r["rect"][1])
        if 3 <= len(members) <= len(ORDINALS):
            k = rng.randrange(len(members))
            out.append(("ordinal", f"the {ORDINALS[k]} {role.lower()} in the list (top to bottom)", members[k]["id"]))
            break
    # beside: the only element on the same line to the right of a uniquely labelled one
    for r in rng.sample(rows, min(len(rows), 30)):
        if not r["label"] or labels[r["label"]] != 1:
            continue
        x, y, w, h = r["rect"]
        right = [o for o in rows if o is not r and o["role"] in TAPPABLE and o["rect"][0] >= x + w
                 and abs((o["rect"][1] + o["rect"][3] / 2) - (y + h / 2)) <= max(1, h / 2)]
        if len(right) == 1 and right[0]["label"] != r["label"]:
            out.append(("beside", f'the {right[0]["role"].lower()} to the right of "{r["label"]}"', right[0]["id"]))
            break
    longs = [r for r in rows if len(r["label"].split()) >= 5 and r["role"] in TAPPABLE]
    if longs:
        r = rng.choice(longs)
        head = " ".join(r["label"].split()[:3])
        if sum(o["label"].startswith(head) for o in rows) == 1:
            out.append(("prefix", f'the {r["role"].lower()} whose label starts with "{head}"', r["id"]))
    return out


def grounding_items(runs, writer, per_screen=2, seed=20261010):
    """Intents on recorded screens (one visit per screen per task), with the screenshot. Also each single-TAP turn's
    own words (the planner's thought) with the element it tapped (kind "thought": the planner's pick, not truth)."""
    rng = random.Random(seed)
    out, seen = [], set()
    for run in runs:
        folder = pathlib.Path(run.folder)
        for p in _prompts(folder):
            text = p.get("text") or ""
            rows = ground_rows(text)
            key = (run.task, T.screen_hash(T.screen_rows(text)))
            if not rows or not p.get("image") or key in seen:
                continue
            seen.add(key)
            image = writer.asset(folder / p["image"])
            if image is None:
                continue
            base = {"task": run.task, "category": run.category, "source_run": run.id, "step": p.get("step"),
                    "app": section(text, "Current app: ", ("\n\n",)), "lines": screen_lines(text), "image": image}
            made = intents(rows, rng)
            rng.shuffle(made)
            for kind, intent, gold in made[:per_screen]:
                out.append({**base, "id": f"{run.id}/s{p.get('step')}/{kind}", "kind": kind, "intent": intent,
                            "gold": gold, "gold_label": next(r["label"] for r in rows if r["id"] == gold)})
            acts = (p.get("out") or {}).get("actions") or []
            if len(acts) == 1 and acts[0].get("operation") == "TAP" and str(acts[0].get("target") or "") in {
                    r["id"] for r in rows} and (p.get("out") or {}).get("thought"):
                gold = acts[0]["target"]
                out.append({**base, "id": f"{run.id}/s{p.get('step')}/thought", "kind": "thought",
                            "intent": p["out"]["thought"], "gold": gold,
                            "gold_label": next(r["label"] for r in rows if r["id"] == gold)})
    return out


# -- the finishing turn ------------------------------------------------------------------------------------------

def finish_items(runs, done_set):
    """Each ending of a frozen DONE set, as the finishing turn the model saw: the final DONE turn's prompt and
    screenshot, with the DONE set's labels (should it have finished here)."""
    _, endings = S.load(done_set)
    by_id = {r.id: r for r in runs}
    out = []
    for e in endings:
        run = by_id.get(e["id"])
        if run is None:
            continue
        prompt = next((p for p in _prompts(run.folder) if p.get("step") == e["done_step"]), None)
        if not prompt or not prompt.get("image"):
            continue
        out.append({"id": e["id"], "task": e["task"], "category": e["category"], "kind": "finish", "step_type": "end",
                    "text": prompt["text"], "operations": prompt.get("operations"), "image": None, "_image_path":
                    str(pathlib.Path(run.folder) / prompt["image"]), "label": e["label"],
                    "gold": {"op": "DONE", "label": "", "text": None, "app": None, "target": None},
                    "alternatives": [], "wrong": None, "screen": None})
    return out


# -- commit classification ---------------------------------------------------------------------------------------

COMPILED = re.compile(r"^(COMMIT|FORBID) (\S+) \(([^)]*)\): (.*?) \| quote=(.*)$")


def compiled_items(lines):
    """Contract items from guard_replay's compiled strings ("COMMIT order (App): what | quote='...'")."""
    import ast
    out = []
    for line in lines:
        m = COMPILED.match(line)
        if m:
            kind, act, app, what, quote = m.groups()
            try:
                quote = ast.literal_eval(quote)
            except (ValueError, SyntaxError):
                pass
            out.append({"kind": kind, "act": act, "app": app, "what": what, "quote": quote})
    return out


def commit_items(runs, replay, labels):
    """Every commit-class control the guard decided on in the recorded runs (each one it refused, each one tapped,
    each one on a screen the model read), once per task, with the contracts guard_replay compiled for that task.
    Labels: ``requested`` (iosworld_stats.commit_requested: True, False or None when it can't tell) and ``required``
    (refused in a run the failure labeller blamed on a guard refusal)."""
    from ..guard_replay import decisions
    from ..iosworld_stats import commit_requested
    compiled = replay.get("compiled") or {}
    blamed = {(row.get("task"), row.get("arm"), row.get("run")) for row in labels if row.get("cause") == "guard_refusal"}
    out = {}
    for run in runs:
        if run.task not in compiled or not (pathlib.Path(run.folder) / "prompts.jsonl").exists():
            continue
        goal, names, decided = decisions(pathlib.Path(run.folder))
        for kind, label, role, app in decided:
            key = (run.task, kind, label, role, app)
            item = out.get(key)
            if item is None:
                digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:10]
                item = out[key] = {"id": f"{run.task}/{kind.replace(' ', '_')}/{digest}", "task": run.task,
                                   "category": run.category, "goal": goal, "kind": kind, "label": label, "role": role,
                                   "app": app, "count": 0, "requested": commit_requested(label, goal),
                                   "required": False, "contracts": [compiled_items(c) for c in compiled[run.task]]}
            item["count"] += 1
            if kind == "refused" and (run.task, run.arm, run.repeat) in blamed:
                item["required"] = True
    return list(out.values())


# -- contract quality ---------------------------------------------------------------------------------------------

def contract_items(runs):
    """One item per task: the request, its apps, and the judge's rubric criteria (with how many judged runs failed
    each: the criteria runs actually miss), plus the contracts recorded in its runs."""
    by = collections.defaultdict(list)
    for r in runs:
        if r.judged and r.criteria:
            by[r.task].append(r)
    out = []
    for task, rs in sorted(by.items()):
        latest = max(rs, key=lambda r: r.started or 0)
        texts = [c["criterion"] for c in latest.criteria]
        failed = collections.Counter()
        counted = collections.Counter()
        for r in rs:
            for c in r.criteria:
                counted[c["criterion"]] += 1
                failed[c["criterion"]] += not c.get("satisfied")
        recorded, seen = [], set()
        for r in sorted(rs, key=lambda r: -(r.started or 0)):
            events = (_record(r.folder).get("mobster") or {}).get("events") or ()
            items = next((e.get("items") for e in events if e.get("event") == "frontier_contract"), None)
            if items and tuple(items) not in seen:
                seen.add(tuple(items))
                recorded.append({"run": r.id, "build": r.build, "items": items})
        prompts = _prompts(latest.folder)
        apps = apps_block(prompts[0].get("text") or "") if prompts else ""
        out.append({"id": task, "task": task, "category": latest.category, "request": latest.goal,
                    "apps": [line.split(": ")[0].lstrip("- ").strip() for line in apps.splitlines() if line.strip()],
                    "criteria": [{"id": i + 1, "text": t, "runs": counted[t], "failed": failed[t]}
                                 for i, t in enumerate(texts)],
                    "recorded": recorded[:4]})
    return [o for o in out if o["apps"]]


# -- the command --------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.build")
    parser.add_argument("set", choices=("done", "value", "golden", "contract", "grounding", "commit", "finish"))
    parser.add_argument("--done-set", help="finish: the frozen DONE set whose endings to take")
    parser.add_argument("--replay", help="commit: guard_replay's output (its compiled contracts are reused)")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--batches", help="comma-separated batch names to keep")
    parser.add_argument("--clean-only", action="store_true", help="only runs on the isolated simulators")
    parser.add_argument("--splits", required=True, help="the tuning loop's frozen splits.json")
    parser.add_argument("--labels", nargs="*", default=(), help="failure-labeller JSONL files (judge misses)")
    parser.add_argument("--root", required=True, help="the sets folder (outside the repository)")
    parser.add_argument("--version", required=True)
    parser.add_argument("--per-task", type=int, default=24, help="golden: path turns per task")
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    splits = json.loads(pathlib.Path(args.splits).read_text())
    halves = S.halves_from_splits(splits)
    runs = T.discover(args.runs, suites=("iosworld",))
    if args.batches:
        keep = set(args.batches.split(","))
        runs = [r for r in runs if r.batch in keep]
    if args.clean_only:
        runs = [r for r in runs if r.clean]
    writer = S.SetWriter(args.root, args.set, args.version, halves, S.forbidden_tasks(splits))
    rng = random.Random(20261010)
    if args.set == "done":
        labels = label_index(args.labels)
        for run in runs:
            item = done_item(run, writer, labels)
            if item:
                writer.add(item)
    elif args.set == "value":
        seen = set()
        for run in runs:
            for item in value_items(run, writer, rng):
                key = (item["task"], T.norm(item["question"]), T.norm(item["gold"]), item["screen"], item["kind"])
                if key not in seen:
                    seen.add(key)
                    writer.add(item)
    elif args.set == "finish":
        for item in finish_items(runs, args.done_set):
            item["image"] = writer.asset(item.pop("_image_path"))
            writer.add(item)
    elif args.set == "commit":
        labels = [row for path in args.labels for row in T.read_jsonl(path)]
        for item in commit_items(runs, json.loads(pathlib.Path(args.replay).read_text()), labels):
            writer.add(item)
    elif args.set == "grounding":
        for item in grounding_items(runs, writer):
            writer.add(item)
    elif args.set == "contract":
        for item in contract_items(runs):
            writer.add(item)
    elif args.set == "golden":
        for item in golden_items(runs, writer, per_task=args.per_task):
            writer.add(item)
    manifest = writer.write(sources=[str(r) for r in args.runs], notes=args.notes)
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reach replay for a change to the contract's proofs: which WRITE and COMMIT items would it flip, in which runs?

    python -m mobile_agent.bench.write_proof_reach --old OLD_REPO --runs DIR... [--kinds WRITE,COMMIT] [--out OUT.json]

Each recorded run's contract is rebuilt from its own turns (the recorded items, each screen as the model read it,
each dispatched action and typed text, each declared commit as the gate let it through, the model's checklist
marks) twice: with the contract module of OLD_REPO and with this one. At the final DONE turn every item of the
chosen kinds has its proof read under both; a flip is proven before and not now (or the reverse). Flips are counted
by the run's official verdict, and each one is listed to be read by hand, with the judge's own reasoning on the
criteria that name its app. $0: no model, no device.
"""

import argparse
import importlib.util
import json
import pathlib
import re
import sys
from types import SimpleNamespace

from .. import contract as NEW

ROW = re.compile(r'^e\d+ (\w+)(?: "((?:[^"\\]|\\.)*)")?(.*?)(?: @(\d+),(\d+),(\d+),(\d+))?$')
VALUE = re.compile(r'value="((?:[^"\\]|\\.)*)"')
COMMIT_OPS = ("TAP", "LONG_PRESS", "SUBMIT", "TYPE_SUBMIT")
ITEM = re.compile(r'^(READ|WRITE|COMMIT|REPORT|FORBID)(?: (\w+))? \(([^)]*)\): (.*?)(?: = "(.*)")?(?: \[[^\]]*\])?$')


def load_old(repo):
    spec = importlib.util.spec_from_file_location("old_contract", pathlib.Path(repo) / "mobile_agent" / "contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["old_contract"] = module
    spec.loader.exec_module(module)
    return module


def elements(text):
    at = text.find("Screen elements:\n")
    out = []
    for line in (text[at + len("Screen elements:\n"):] if at >= 0 else "").splitlines():
        m = ROW.match(line.strip())
        if not m or not line.startswith("e"):
            continue
        rest = m.group(3) or ""
        value = VALUE.search(rest)
        marks = VALUE.sub("", rest)  # a row's marks, outside its value's quotes: editable, selected
        out.append(SimpleNamespace(label=(m.group(2) or "").replace('\\"', '"'), role=m.group(1),
                                   rect=tuple(int(m.group(i)) / 100 for i in range(4, 8)) if m.group(4) else (0, 0, 0, 0),
                                   editable=" editable" in marks, selected=" selected" in marks,
                                   value=value.group(1).replace('\\"', '"') if value else ""))
    return out


def rebuild(K, folder):
    """The run's contract under module ``K``, replayed to its last turn; None without a recorded contract."""
    record = json.loads((folder / "task.json").read_text())
    events = (record.get("mobster") or {}).get("events") or []
    labels = next((e.get("items") for e in events if e.get("event") == "frontier_contract"), None)
    if not labels:
        return None
    raw = []
    for label in labels:
        m = ITEM.match(label)
        if m:
            kind, act, app, what, payload = m.groups()
            raw.append({"kind": kind, "act": act or "none", "app": app, "what": what, "payload": payload or "",
                        "count": "1", "condition": "", "quote": record.get("goal") or ""})
    contract = K.Contract.from_items(raw, record.get("goal") or "")
    contract.unframed = set()  # ids of items whose commit control the replay took by label alone, with no frame
    actions = {}
    for e in events:
        if e.get("event") == "frontier_action":
            actions.setdefault(e.get("step"), []).append(e)
    turns = [json.loads(line) for line in (folder / "prompts.jsonl").read_text().splitlines() if line.strip()]
    for turn in turns:
        step, text = turn.get("step"), turn.get("text") or ""
        app = (re.search(r"Current app: (.*)", text) or [None, ""])[1].strip()
        contract.observe(SimpleNamespace(elements=elements(text), keyboard=""), app, step)
        contract.update((turn.get("out") or {}).get("checklist_updates"), step)
        screen = SimpleNamespace(elements=elements(text), keyboard="", bundle_id="")
        chain = []
        for e in actions.get(step, ()):
            where = e.get("app_name") or app
            operation, label = e.get("operation"), e.get("target_label") or ""
            # A declared commit as the gate let it through, on the screen of its turn: it awaits its receipt. A
            # control that appeared mid-chain (a composer's Send once text is typed) is not on that screen: it is
            # taken by its label alone.
            target = next((x for x in screen.elements if x.label == label), None) if label else None
            if target is None and label and operation in COMMIT_OPS:
                target = SimpleNamespace(label=label, role="Button", rect=(0, 0, 0, 0), editable=False, value="")
            gate = (contract.check(operation, target, screen, where, text=e.get("text"), chain_typed=tuple(chain))
                    if target is not None and operation in COMMIT_OPS else None)
            if e.get("text") and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
                chain.append(e["text"])
            if hasattr(contract, "record_action"):
                contract.record_action(where, operation, label)
            if e.get("text") and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
                field = SimpleNamespace(label="search" if e.get("field") == "search" else "", role="TextField",
                                        rect=(0, 0, 0, 0), editable=True, value="")
                contract.record_typed(where, field, e["text"], step)
            if gate is not None and gate.allowed and gate.item is not None:
                contract.committed(gate, target, screen, where, step)
                if target.rect == (0, 0, 0, 0):
                    # The live run had this control's frame; a proof that reads where it was can't be replayed.
                    contract.unframed.add(gate.item.id)
    return contract


def judged_on(folder, app):
    """[(criterion, satisfied)] for the judge's criteria that name ``app``: what the judge said of the item's part."""
    evaluation = json.loads((folder / "task.json").read_text()).get("evaluation") or {}
    word = (app or "").casefold()
    return [(r.get("criterion") or "", r.get("satisfied")) for r in evaluation.get("rubric_results") or ()
            if word and word in (r.get("criterion") or "").casefold()]


def main(argv=None):
    from .iosworld_stats import load_task
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.write_proof_reach")
    parser.add_argument("--old", required=True, help="a checkout whose contract.py is the rule before the change")
    parser.add_argument("--runs", nargs="+", required=True, help="run folders (each holding NNN-task/ folders)")
    parser.add_argument("--kinds", default="WRITE,COMMIT", help="item kinds to compare (default WRITE,COMMIT)")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    kinds = {k.strip().upper() for k in args.kinds.split(",") if k.strip()}
    OLD = load_old(args.old)
    counts, flips = {}, []
    for root in args.runs:
        for task_json in sorted(pathlib.Path(root).glob("*/task.json")):
            folder = task_json.parent
            run = load_task(str(task_json))
            if not run.judged or not (folder / "prompts.jsonl").exists():
                continue
            old, new = rebuild(OLD, folder), rebuild(NEW, folder)
            if old is None:
                continue
            group = "pass" if run.official else "fail"
            for a, b in zip(old.items, new.items):
                if a.kind not in kinds:
                    continue
                before, after = a and old.proof(a)[0], new.proof(b)
                key = (a.kind, group, "proven" if before else "open", "proven" if after[0] else "open")
                counts[key] = counts.get(key, 0) + 1
                if bool(before) != bool(after[0]):
                    flips.append({"group": group, "run": f"{folder.parent.parent.name}/{folder.parent.name}/{folder.name}",
                                  "item": b.label(), "before": bool(before), "after": after[0], "reason": after[1],
                                  "claim": b.claim, "value": b.value, "receipts": b.receipts,
                                  "unframed": b.id in new.unframed, "judge": judged_on(folder, b.app)})
    summary = {"counts": {"/".join(k): v for k, v in sorted(counts.items())},
               "flips_pass": sum(f["group"] == "pass" for f in flips), "flips_fail": sum(f["group"] == "fail" for f in flips)}
    print(json.dumps(summary, indent=1))
    for f in flips:
        print(f"{f['group']:4} {f['run']:34} {'proven->open' if f['before'] else 'open->proven'} {f['item'][:70]} "
              f"| {f['reason'][:60]} | claim={f['claim']} value={str(f['value'])[:40]!r}")
        if f["unframed"]:
            print("       its control appeared mid-chain: the replay has its label, not its frame")
        for criterion, satisfied in f["judge"]:
            print(f"       judge {'yes' if satisfied else 'no ' if satisfied is False else '?  '} {criterion[:110]}")
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"summary": summary, "flips": flips}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

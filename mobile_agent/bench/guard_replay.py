"""Offline check of a change to the commit guard's families: what would it newly allow, on recorded runs?

    python -m mobile_agent.bench.guard_replay --runs DIR... --model claude-sonnet-5-5 --compiles 2 --out OUT.json \\
        --env-file bench.env

For each recorded task, the request's contract is compiled ``--compiles`` times (contract.compile_contract, as the
loop does). Every recorded gate decision is then replayed under the old and the new family rule: each control the
guard refused, each commit-class control the run tapped, and every commit-class control on every screen the model
read. The guard's other checks (amount bounds, typed text, object names, approvals) are unchanged by a family change
and are not replayed: the question is only which controls' verbs would now count as declared.

A control newly allowed is listed with whether the request asks for its act (iosworld_stats.commit_requested, an
approximation) so each can be read by hand. The change passes when no newly allowed control is unrequested.
"""

import argparse
import collections
import json
import pathlib
import re

from .. import contract as K
from .iosworld_stats import NON_GUARD_REFUSALS, commit_requested

ROW = re.compile(r'^e\d+ (\w+) "([^"]*)"', re.M)
OTHER_REFUSALS = NON_GUARD_REFUSALS | {"tap_xy", "untargeted_swipe", "disabled", "passcode_keypad", "needs_approval"}


def old_family(item):
    """The family rule before the request's own word for the act counted (contract.py at e24a7f9f)."""
    fam = set(K.FAMILIES.get(item.act, ()))
    if item.act == "other":
        fam |= {v.casefold() for v in K.COMMIT_WORDS.findall(item.quote)}
    return fam


def declared(contract, verbs, app, family):
    return any(i.kind == "COMMIT" and K.Contract._app_ok(i, app) and verbs <= family(i) for i in contract.items)


def forbidden(contract, verbs, app):
    return any(i.kind == "FORBID" and K.Contract._app_ok(i, app) and verbs & i.family() for i in contract.items)


def decisions(folder):
    """(goal, app names, [(kind, label, role, app)]) from one recorded run."""
    record = json.loads((folder / "task.json").read_text())
    prompts = [json.loads(line) for line in (folder / "prompts.jsonl").read_text().splitlines() if line.strip()]
    app_at, names, out = {}, [], []
    for p in prompts:
        text = p.get("text") or ""
        current = text.split("Current app: ", 1)[1].split("\n", 1)[0].strip() if "Current app: " in text else ""
        app_at[p.get("step")] = current
        if not names and "Apps you may use:\n" in text:
            names = [line.split(": ")[0].lstrip("- ").strip() for line in
                     text.split("Apps you may use:\n", 1)[1].split("\n\n")[0].splitlines()]
        at = text.find("Screen elements:")
        for role, label in ROW.findall(text[at:] if at >= 0 else ""):
            if K.label_verbs(label, role):
                out.append(("on screen", label, role, current))
    for e in (record.get("mobster") or {}).get("events") or ():
        if e.get("event") == "frontier_refused" and e.get("label") not in OTHER_REFUSALS:
            out.append(("refused", e.get("label") or "", "Button", app_at.get(e.get("step"), "")))
        elif e.get("event") == "frontier_action" and e.get("operation") in ("TAP", "LONG_PRESS") and K.label_verbs(
                e.get("target_label") or ""):
            out.append(("tapped", e.get("target_label") or "", "Button", e.get("app_name") or app_at.get(e.get("step"), "")))
    return record.get("goal") or "", names, out


def main(argv=None):
    from ..frontier import chat_client, cost_usd
    from .iosworld import load_envs
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.guard_replay")
    parser.add_argument("--runs", nargs="+", required=True, help="run folders (each holding NNN-task/ folders)")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--compiles", type=int, default=2)
    parser.add_argument("--env-file", action="append")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    load_envs(args.env_file)
    client = chat_client(args.model, reasoning="low")
    by_task = collections.defaultdict(list)
    for root in args.runs:
        for task_json in sorted(pathlib.Path(root).glob("*/task.json")):
            if (task_json.parent / "prompts.jsonl").exists():
                by_task[task_json.parent.name.split("-", 1)[1]].append(task_json.parent)
    newly, counts, compiled = [], collections.Counter(), {}
    for task, folders in sorted(by_task.items()):
        goal, names, _ = decisions(folders[0])
        contracts = []
        for _ in range(args.compiles):
            c = K.compile_contract(client, goal, names)
            if c is not None:
                contracts.append(c)
        compiled[task] = [[f"{i.kind} {i.act} ({i.app}): {i.what} | quote={i.quote!r}" for i in c.items if i.kind in
                           ("COMMIT", "FORBID")] for c in contracts]
        seen = set()
        for folder in folders:
            _, _, decided = decisions(folder)
            for kind, label, role, app in decided:
                verbs = frozenset(K.label_verbs(label, role))
                if not verbs:
                    continue
                for index, c in enumerate(contracts):
                    counts["decisions"] += 1
                    old = declared(c, verbs, app, old_family) and not forbidden(c, verbs, app)
                    new = declared(c, verbs, app, lambda i: i.family()) and not forbidden(c, verbs, app)
                    counts[f"{kind}: old {'allow' if old else 'refuse'} -> new {'allow' if new else 'refuse'}"] += 1
                    if new and not old and (task, label, app, index) not in seen:
                        seen.add((task, label, app, index))
                        newly.append({"task": task, "kind": kind, "label": label, "app": app, "compile": index,
                                      "verbs": sorted(verbs), "requested": commit_requested(label, goal),
                                      "goal": goal})
    usd = cost_usd(client.model, client.usage) or 0.0
    result = {"tasks": len(by_task), "counts": dict(counts), "newly_allowed": newly,
              "newly_allowed_unrequested": [n for n in newly if n["requested"] is not True],
              "compiled": compiled, "usd": round(usd, 4)}
    pathlib.Path(args.out).write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps({k: result[k] for k in ("tasks", "counts", "usd")}, indent=1))
    for n in newly:
        print(f"NEWLY ALLOWED {n['task']:14} {n['kind']:9} {n['app']:10} {n['label'][:60]!r} requested={n['requested']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

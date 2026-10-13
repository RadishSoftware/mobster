"""Would programs save model calls? An offline estimate over recorded frontier turns.

Today each call returns 1-6 actions, run until a target is missing or nothing changes, and 46% of
the next turns' targets were already on screen (25 Sep): the model stops to look where a condition
("if Confirm appears, tap it") or an expectation would let it go on. This re-asks recorded turns
with a program format (steps that may be conditional on a visible label, expect a label after
them, or repeat while a label is visible) and walks each program against what the run actually did
from that turn on:

    saved      later turns whose every action the program would have done, in order
    diverged   programs whose unconditional step would have done something the run did not

No simulator; a few dollars for a few hundred turns:

    python -m mobile_agent.bench.program_estimate research/iosworld-runs/ab4-new-a --fraction .25 --out prog.jsonl
    python -m mobile_agent.bench.program_estimate --report prog.jsonl
"""

import argparse
import collections
import json
import os
import pathlib

from .. import frontier
from .iosworld import load_envs
from .replay import rebuilt, turns
from .sweep import sampled, task_folders

PROGRAM_OPS = ("TAP", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT",
               "SWIPE_RIGHT", "DISMISS", "HOME", "LAUNCH_APP", "DONE", "BLOCKED")
MAX_STEPS = 14

PROGRAM_SYSTEM = frontier.SYSTEM + """

PROGRAM FORMAT (replaces "Answer with a short sequence of actions"): answer with a program of up to 14 \
steps that code runs for you without asking you again, as long as the screen goes as you expect. Plan as \
far ahead as you can predict: every step whose target you can name by its exact label on the screen it \
will be on. Each step:
- op, target (an element id on the current screen, or the exact label it will have), text, app;
- if_visible: a label; the step runs only if an element with that label is on screen then, else it is \
skipped (e.g. tap "Confirm" if a confirmation appears);
- expect: a label that must be on screen after the step; if it is not, the program stops and you see \
the screen (use it where you are unsure the step works);
- repeat: true to run the step again while its target is still on screen (at most 8 times: e.g. every \
"Pay" in a list).
The program stops by itself at a missing target or a failed expect. DONE or BLOCKED may only be last."""


def program_schema(apps):
    step = {
        "type": "object", "additionalProperties": False,
        "required": ["op", "target", "text", "app", "if_visible", "expect", "repeat"],
        "properties": {
            "op": {"type": "string", "enum": list(PROGRAM_OPS)},
            "target": {"type": ["string", "null"]}, "text": {"type": ["string", "null"]},
            "app": {"type": ["string", "null"], "enum": list(apps) + [None]},
            "if_visible": {"type": ["string", "null"]}, "expect": {"type": ["string", "null"]},
            "repeat": {"type": "boolean"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["thought", "plan", "program", "answer"],
            "properties": {"thought": {"type": "string"}, "plan": {"type": "string"},
                           "program": {"type": "array", "items": step, "minItems": 1, "maxItems": MAX_STEPS},
                           "answer": {"type": ["string", "null"]}}}


def recorded_actions(folder):
    """{turn: [(operation, target label or app)]} of the actions the run executed, in order."""
    record = json.loads((pathlib.Path(folder) / "task.json").read_text(encoding="utf-8"))
    done = collections.defaultdict(list)
    for event in record["mobster"].get("events") or ():
        if event.get("event") == "frontier_action":
            done[event["step"]].append((event.get("operation"), event.get("target_label") or ""))
        elif event.get("event") == "frontier_decision" and event.get("ops") and event["ops"][-1] in ("DONE", "BLOCKED"):
            done[event["step"]].append((event["ops"][-1], ""))
    return done


def _same(label, want):
    label, want = " ".join((label or "").split()).casefold(), " ".join((want or "").split()).casefold()
    return bool(label and want) and (label == want or label.startswith(want) or want.startswith(label))


def _family(op):
    """Operations that do the same thing (typing a field's text, with or without Return) match."""
    return "TYPE" if op in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") else op


def target_label(step, text):
    """A step's target as a label: an id on the prompt's screen is looked up in its rows."""
    target = step.get("target") or ""
    for line in text.splitlines():
        if target and line.startswith(target + " "):
            parts = line.split(" ", 2)
            return parts[2].rsplit(" @", 1)[0].strip('"') if len(parts) > 2 else ""
    return target


def walk(program, text, done, start):
    """(recorded later turns the program covers in full, whether an unconditional step diverged)."""
    flat = [(turn, op, label) for turn in sorted(t for t in done if t >= start) for op, label in done[turn]]
    at = 0
    for step in program:
        op = step["op"]
        want = step.get("app") if op == "LAUNCH_APP" else target_label(step, text)
        runs = 0
        while at < len(flat):
            _, got_op, got_label = flat[at]
            if _family(got_op) == _family(op) and (op not in frontier.ELEMENT_OPS | {"LAUNCH_APP"}
                                                   or _same(got_label, want)):
                at, runs = at + 1, runs + 1
                if step.get("repeat") and runs < 8:
                    continue
                break
            if runs or step.get("if_visible"):
                break  # a repeat ended, or a condition taken as false
            covered = {turn for turn, _, _ in flat[:at]} - {start}
            return sum(1 for turn in covered if all(t != turn for t, _, _ in flat[at:])), True
        if at >= len(flat):
            break
    covered = {turn for turn, _, _ in flat[:at]} - {start}
    return sum(1 for turn in covered if all(t != turn for t, _, _ in flat[at:])), False


CONTROL_ROLES = ("Button", "TextField", "SearchField", "Switch", "Cell", "Tab", "SegmentedControl", "Link")


def screen_of(text):
    """(app, {short control labels}) of a prompt's screen: its identity, whatever the list rows say."""
    app = next((line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Current app: ")), "")
    labels = set()
    for line in text.split("Screen elements:\n")[-1].splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[1] in CONTROL_ROLES and parts[2].startswith('"'):
            label = parts[2][1:].split('"', 1)[0]
            if label and len(label.split()) <= 4:
                labels.add(label)
    return app, labels


def build_atlas(paths, leave_out=()):
    """[(app, screen labels, op, target label, next screen labels)] from recorded turns that did one
    action, skipping tasks in ``leave_out`` (a task is never helped by its own runs)."""
    atlas = []
    for folder in task_folders(paths):
        if folder.name.split("-", 1)[-1] in leave_out:
            continue
        records = {t["step"]: t for t in turns(folder)}
        done = recorded_actions(folder)
        for step, record in records.items():
            nxt = records.get(step + 1)
            if nxt is None or len(done.get(step, [])) != 1:
                continue
            op, label = done[step][0]
            if op not in ("TAP", "LAUNCH_APP") or not label:
                continue
            app, here = screen_of(record["text"])
            _, there = screen_of(nxt["text"])
            if here and there and here != there:
                atlas.append((app, frozenset(here), op, label, frozenset(there)))
    return atlas


def atlas_lines(atlas, text, limit=10):
    """What earlier use says the current screen's controls lead to."""
    app, here = screen_of(text)
    known = collections.defaultdict(collections.Counter)
    for a_app, a_here, op, label, there in atlas:
        if a_app == app and label in here and len(a_here & here) >= .5 * len(a_here | here):
            known[(op, label)].update(there - here)
    lines = []
    for (op, label), leads in sorted(known.items(), key=lambda kv: -sum(kv[1].values()))[:limit]:
        lines.append(f'- {op} "{label}" led to a screen with: ' + ", ".join(f'"{l}"' for l, _ in leads.most_common(12)))
    return lines


def run(paths, out_path, *, model, reasoning, fraction, seed, max_usd, atlas_paths=None):
    client = frontier.chat_client(model, reasoning=reasoning)
    out_file = pathlib.Path(out_path)
    seen = {json.loads(line)["key"] for line in out_file.read_text().splitlines()} if out_file.exists() else set()
    with out_file.open("a", encoding="utf-8") as sink:
        for folder in task_folders(paths):
            done = recorded_actions(folder)
            task = folder.name.split("-", 1)[-1]
            atlas = build_atlas(atlas_paths, leave_out={task}) if atlas_paths else None
            for record in turns(folder):
                name = f"{folder.parent.name}/{folder.name}:{record['step']}"
                if name in seen or not sampled(name, fraction, seed) or not record.get("out"):
                    continue
                spent = frontier.cost_usd(model, client.usage) or 0
                if spent >= max_usd:
                    print(f"spend cap reached (${spent:.2f})")
                    return
                messages, _ = rebuilt(record, folder)
                messages[0]["content"] = PROGRAM_SYSTEM
                known = atlas_lines(atlas, frontier.prompt_text(messages)) if atlas else []
                if known:
                    part = next(p for p in reversed(messages[1]["content"]) if p.get("type") == "text")
                    part["text"] += ("\n\nFrom earlier use of this app (may be out of date; names the screens a "
                                     "control led to, so you can plan past them):\n" + "\n".join(known))
                try:
                    out, _ = client.complete(messages, program_schema(record["apps"]), timeout=90)
                except Exception as error:
                    print(name, "failed:", str(error)[:120])
                    continue
                text = frontier.prompt_text(messages)
                saved, diverged = walk(out.get("program") or [], text, done, record["step"])
                _, here = screen_of(text)
                beyond = sum(1 for step in (out.get("program") or [])[1:] if step["op"] in ("TAP", "TYPE", "TYPE_SUBMIT",
                             "SET_TEXT") and target_label(step, text) not in here)
                sink.write(json.dumps({"key": name, "steps": len(out.get("program") or []), "saved": saved,
                                       "beyond": beyond, "atlas_lines": len(known),
                                       "diverged": diverged, "program": out.get("program"),
                                       "recorded_turns_left": len([t for t in done if t > record["step"]])},
                                      ensure_ascii=False) + "\n")
                sink.flush()
    print(f"spent ${frontier.cost_usd(model, client.usage) or 0:.2f}")


def report(path):
    rows = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    if not rows:
        print("no rows")
        return
    saved = sum(r["saved"] for r in rows)
    print(f"{len(rows)} programs, mean {sum(r['steps'] for r in rows) / len(rows):.1f} steps; "
          f"later turns covered: {saved} ({saved / len(rows):.2f} per program call); "
          f"diverged: {sum(r['diverged'] for r in rows)} ({100 * sum(r['diverged'] for r in rows) / len(rows):.0f}%)")
    print(f"calls per turn of work if every program ran: {len(rows) / (len(rows) + saved):.2f} "
          f"(1.00 today; lower is fewer model calls)")
    helped = [r for r in rows if r.get("atlas_lines")]
    print(f"steps past the current screen: {sum(r.get('beyond', 0) for r in rows) / len(rows):.2f} per program; "
          f"turns with atlas lines: {len(helped)}"
          + (f", their mean steps {sum(r['steps'] for r in helped) / len(helped):.1f}" if helped else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--out", default="program-estimate.jsonl")
    parser.add_argument("--report")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("--fraction", type=float, default=.25)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-usd", type=float, default=2.0)
    parser.add_argument("--atlas", nargs="*", help="run folders to learn an app map from (each task's own runs left out)")
    parser.add_argument("--env-file", action="append", default=["mobile_agent/.env"])
    args = parser.parse_args(argv)
    if args.report:
        report(args.report)
        return
    load_envs([path for path in args.env_file if os.path.exists(os.path.expanduser(path))])
    run(args.paths, args.out, model=args.model, reasoning=args.reasoning, fraction=args.fraction, seed=args.seed,
        max_usd=args.max_usd, atlas_paths=args.atlas)
    report(args.out)


if __name__ == "__main__":
    main()

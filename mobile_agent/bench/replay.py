"""Replay one recorded frontier decision: cents per try, no simulator.

A run's prompts.jsonl holds, per turn, the exact text and image the model saw and what it chose.
Replaying a turn re-asks the model with the current SYSTEM prompt (or one from a file), laid out
as frontier.prompt_messages lays it out today (the recorded fields, re-read by ``parse``), so a
prompt fix is tested on the real failing decision, several times, before any rerun:

    python -m mobile_agent.bench.replay research/iosworld-runs/fix-2/091-mem-004 --turn 9 -n 3
    python -m mobile_agent.bench.replay ... --turn 9 --system edited_system.txt
    python -m mobile_agent.bench.replay ... --turn 39 --feedback DONE_CHECK   # new feedback on that turn
    python -m mobile_agent.bench.replay ... --list          # every turn's first action, compactly
"""

import argparse
import base64
import json
import os
import pathlib
import re

from .. import frontier
from .iosworld import load_envs


def turns(folder):
    path = pathlib.Path(folder) / "prompts.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def target_line(text, alias):
    """The screen row the model's element id names, from the prompt text."""
    return next((line for line in text.splitlines() if alias and line.startswith(alias + " ")), None)


def describe(out, text):
    """The chosen actions, with each element id resolved to its screen row."""
    parts = []
    for act in (out or {}).get("actions") or ():
        row = target_line(text, act.get("target"))
        what = (row or act.get("target_label") or act.get("app") or "")[:90]
        parts.append(f"{act.get('operation')} {what}" + (f" text={act['text']!r}" if act.get("text") else ""))
    if (out or {}).get("answer"):
        parts.append("answer=" + json.dumps(out["answer"], ensure_ascii=False))
    return " | ".join(parts)


SECTIONS = (("request", "Request: "), ("turns", "Turns used: "), ("apps", "Apps you may use:\n"),
            ("current", "Current app: "), ("plan", "Plan so far: "), ("notes", "Notes:\n"),
            ("checklist", "Checklist:\n"), ("listing", frontier.LISTING_HEAD),
            ("recent", "Recent actions (oldest first):\n"), ("feedback", "Feedback on your last action: "),
            ("screen", "Screen elements:\n"))
ROW = re.compile(r"^(e\d+) (\S+) ")


def parse(text):
    """A recorded prompt's fields, from either layout (the turn counter before or after the apps)."""
    marks = sorted((0 if name == "request" else text.find("\n\n" + head) + 2, name, head)
                   for name, head in SECTIONS if name == "request" or "\n\n" + head in text)
    fields = {}
    for index, (at, name, head) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        fields[name] = text[at + len(head):end].strip("\n")
    listed = lambda value, empty: [] if value in (empty, "") else value.split("\n")
    return {"request": fields["request"], "turns": fields.get("turns", ""), "apps": fields.get("apps", ""),
            "current": fields.get("current", ""), "plan": "" if fields.get("plan") == "(none)" else fields.get("plan", ""),
            "notes": [n[2:] for n in listed(fields.get("notes", ""), "(none)")],
            "checklist": fields.get("checklist"), "listing": fields.get("listing"),
            "recent": listed(fields.get("recent", ""), "(none yet)"), "feedback": fields.get("feedback"),
            "rows": listed(fields.get("screen", ""), None)}


class Row:
    """A recorded screen row as lean_skip reads it."""

    def __init__(self, line):
        match = ROW.match(line)
        self.line, self.alias, self.role = line, match[1] if match else None, match[2] if match else ""


def rows_for(lines, lean):
    """(prompt lines re-aliased e1.., {new alias: recorded alias}) with lean_skip's rows left out if ``lean``."""
    rows = [Row(line) for line in lines if line != frontier.KEYBOARD_ROW]
    skip = frontier.lean_skip(rows) if lean else set()
    out, back = [], {}
    for index, row in enumerate(rows):
        if index in skip or row.alias is None:
            continue
        alias = f"e{len(back) + 1}"
        back[alias] = row.alias
        out.append(alias + row.line[len(row.alias):])
    if any(rows[index].role == "Key" for index in skip) or frontier.KEYBOARD_ROW in lines:
        out.append(frontier.KEYBOARD_ROW)
    return out, back


def rebuilt(record, folder, *, lean=frontier.LEAN_ROWS, feedback=None):
    """(messages in today's layout for a recorded turn, {alias: recorded alias})."""
    fields = parse(record["text"])
    lines, back = rows_for(fields["rows"], lean)
    image = None
    if record.get("image"):
        data = base64.b64encode((pathlib.Path(folder) / record["image"]).read_bytes()).decode()
        image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data, "detail": "low"}}
    extra = ((f"\n\nChecklist:\n{fields['checklist']}" if fields["checklist"] else "")
             + (f"\n\n{frontier.LISTING_HEAD}{fields['listing']}" if fields["listing"] else ""))
    messages = frontier.prompt_messages(fields["request"], fields["apps"], fields["turns"], fields["current"],
                                        fields["plan"], fields["notes"], fields["recent"],
                                        feedback or fields["feedback"], lines, image, extra=extra)
    return messages, back


def replay(folder, turn, *, system=None, feedback=None, model="gpt-5.6-terra", reasoning="low", n=1):
    record = next(t for t in turns(folder) if t["step"] == turn)
    messages, _ = rebuilt(record, folder, feedback=feedback)
    if system:
        messages[0]["content"] = system
    schema = frontier._schema(record["apps"], record["operations"])
    client = frontier.chat_client(model, reasoning=reasoning)
    text = frontier.prompt_text(messages)
    print(f"recorded: {describe(record['out'], record['text'])}\n          {record['out'].get('thought', '')[:200]}")
    for attempt in range(n):
        out, _ = client.complete(messages, schema, timeout=90)
        print(f"replay {attempt + 1}: {describe(out, text)}\n          {out.get('thought', '')[:200]}")
    cost = frontier.cost_usd(model, client.usage)
    print(f"({n} call(s), ${cost:.4f})" if cost is not None else f"({n} call(s))")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_folder")
    parser.add_argument("--turn", type=int)
    parser.add_argument("--list", action="store_true", help="print each turn's recorded choice")
    parser.add_argument("--show", action="store_true", help="print the turn's full prompt text")
    parser.add_argument("--system", help="file with a SYSTEM prompt to try instead of the current one")
    parser.add_argument("--feedback", help="feedback text to show on this turn (e.g. frontier.DONE_CHECK)")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("-n", type=int, default=1)
    parser.add_argument("--env-file", action="append", default=["mobile_agent/.env"])
    args = parser.parse_args(argv)
    if args.list:
        for record in turns(args.task_folder):
            print(f"{record['step']:3} {describe(record['out'], record['text'])[:200]}")
        return
    if args.show:
        print(next(t for t in turns(args.task_folder) if t["step"] == args.turn)["text"])
        return
    load_envs([path for path in args.env_file if os.path.exists(os.path.expanduser(path))])
    system = pathlib.Path(args.system).read_text(encoding="utf-8") if args.system else None
    feedback = frontier.DONE_CHECK if args.feedback == "DONE_CHECK" else args.feedback
    replay(args.task_folder, args.turn, system=system, feedback=feedback, model=args.model, reasoning=args.reasoning,
           n=args.n)


if __name__ == "__main__":
    main()

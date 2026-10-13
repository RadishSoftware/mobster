"""Recorded runs in one shape: a Run of Turns of Actions, from either benchmark's folders. $0, no device.

Two sources:

- **iOSWorld task folders** (``bench/iosworld.py``): ``NNN-<task>/task.json`` (the request, the judge's verdict
  and the run's ``mobster.events``) and ``prompts.jsonl`` (one line per model turn: the prompt text the model saw,
  its reply and the screenshot it saw). A batch folder holds run folders (``A1``, ``ab7-a1``...) of task folders.
- **Phone and simulator benchmark runs** (``bench/runner.py``): ``records.jsonl`` (one line per attempt, with the
  verdict) and ``artifacts/<repeat>_<task>_<agent>/`` holding ``events.jsonl`` and ``prompts.jsonl``.

A turn's screen is identified as ``iosworld_stats`` does it: a hash of the first 40 (role, label) rows of the
prompt's element list, so values typed into a field don't change it. Timing comes from the run's events:
``frontier_decision`` (the model call) and ``frontier_action`` (refresh, exec and settle per action). The adapter
keeps only the last 400 events of a run, so a long run's early turns can lack timing (``Run.events_capped``).
"""

import dataclasses
import glob
import hashlib
import json
import os
import pathlib
import re
import time

from ..iosworld_stats import EVENTS_CAP, NON_GUARD_REFUSALS, official_pass, rubric_score

ROW = re.compile(r'^(e\d+) (\w+) "([^"]*)"', re.M)
CURRENT_APP = re.compile(r"^Current app: (.*)$", re.M)
# The pooled simulators were rebuilt and isolated on 26 Sep 2026 (PR #17): runs before then shared app data
# between workers, so their outcomes are suspect even where their timings are fine.
CLEAN_SINCE = time.mktime((2026, 9, 26, 16, 40, 0, 0, 0, -1))
TYPED_OPS = frozenset({"TYPE", "TYPE_SUBMIT", "SET_TEXT"})
SWITCH_OPS = frozenset({"LAUNCH_APP", "HOME", "OPEN_URL"})
SCROLL_OPS = frozenset({"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "SCROLL_TO", "READ_LIST"})
END_OPS = frozenset({"DONE", "BLOCKED"})
LONG_TEXT = 40  # typed text longer than this is free-form content (a note's body), compared only as "long text"
BACK_WORDS = frozenset({"back", "cancel", "close", "dismiss", "x", "×", "not now", "later"})


def norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def app_key(app):
    """An app's comparable name: "CloudSheets" and "com.example.cloudsheets" are both "cloudsheets"."""
    app = norm(app)
    return app.rsplit(".", 1)[-1] if "." in app and " " not in app else app


def step_type(op):
    """The kind of step an operation is: ground (tap an element), type, scroll (move or read a list), switch (change
    app), end (DONE or BLOCKED), other (WAIT, DISMISS...)."""
    if op in TYPED_OPS or op == "SUBMIT":
        return "type"
    if op in ("TAP", "LONG_PRESS", "TAP_XY"):
        return "ground"
    if op in SCROLL_OPS:
        return "scroll"
    if op in SWITCH_OPS:
        return "switch"
    if op in END_OPS:
        return "end"
    return "other"


@dataclasses.dataclass
class Action:
    op: str
    label: str = ""
    target: str = None
    text: str = None
    app: str = None
    changed: bool = None
    refresh_ms: float = None
    exec_ms: float = None
    settle_ms: float = None
    executed: bool = False

    @property
    def device_ms(self):
        return sum(x or 0.0 for x in (self.refresh_ms, self.exec_ms, self.settle_ms))

    @property
    def is_back(self):
        return self.op == "DISMISS" or (self.op in ("TAP", "TAP_XY") and norm(self.label) in BACK_WORDS)

    def token(self):
        """What the action did, comparable across runs of the same task: (app, operation, label, typed text)."""
        if self.op in SWITCH_OPS:
            return (app_key(self.app), self.op, app_key(self.text or self.label), "")
        text = norm(self.text) if self.op in TYPED_OPS else ""
        return (app_key(self.app), self.op, norm(self.label), text if len(text) <= LONG_TEXT else "<long text>")


@dataclasses.dataclass
class Turn:
    step: int
    app: str = None
    screen: str = None
    title: str = None
    rows: int = 0
    planned: list = dataclasses.field(default_factory=list)
    actions: list = dataclasses.field(default_factory=list)
    refused: list = dataclasses.field(default_factory=list)
    failed: list = dataclasses.field(default_factory=list)
    stops: list = dataclasses.field(default_factory=list)
    route: str = None
    model_ms: float = None
    ttft_ms: float = None
    gen_ms: float = None
    prep_ms: float = None
    in_tokens: int = None
    out_tokens: int = None
    cached_tokens: int = None
    write_tokens: int = None
    out_chars: int = None
    notes_add: int = 0
    checklist: list = dataclasses.field(default_factory=list)
    answer: str = None
    thought: str = None
    image: str = None
    has_prompt: bool = False
    timed: bool = False

    @property
    def done(self):
        return any(a.op in END_OPS for a in self.planned)

    @property
    def done_only(self):
        return bool(self.planned) and all(a.op in END_OPS for a in self.planned)

    @property
    def gained(self):
        """The turn wrote something down: a note, a checklist item found or done, an answer."""
        return bool(self.notes_add or self.answer or any(
            (c or {}).get("status") in ("found", "done") for c in self.checklist))

    @property
    def device_ms(self):
        return sum(a.device_ms for a in self.actions)

    @property
    def seconds(self):
        return ((self.model_ms or 0.0) + (self.prep_ms or 0.0) + self.device_ms) / 1000

    @property
    def kind(self):
        ops = [a.op for a in (self.actions or self.planned)]
        return step_type(ops[0]) if ops else "other"


@dataclasses.dataclass
class Run:
    folder: str
    suite: str
    batch: str
    arm: str
    repeat: int
    task: str
    category: str = None
    difficulty: str = None
    apps: list = dataclasses.field(default_factory=list)
    goal: str = ""
    model: str = None
    build: str = None
    device: str = "sim"
    clean: bool = True
    judged: bool = False
    passed: bool = None
    rubric: float = None
    criteria: list = dataclasses.field(default_factory=list)
    status: str = None
    reason: str = ""
    answer: str = ""
    agent_s: float = None
    usd: float = None
    calls: int = None
    timing: dict = dataclasses.field(default_factory=dict)
    events_capped: bool = False
    started: float = None
    turns: list = dataclasses.field(default_factory=list)

    @property
    def id(self):
        return f"{self.batch}/{self.arm}{self.repeat}/{self.task}"

    @property
    def actions(self):
        return [a for t in self.turns for a in (t.actions or t.planned) if a.op not in END_OPS]

    @property
    def timed(self):
        return any(t.timed for t in self.turns)


# -- prompts ---------------------------------------------------------------------------------------

def screen_rows(text):
    """[(id, role, label)] of the prompt's element list ("Screen elements:" to the end of the text)."""
    at = (text or "").find("Screen elements:")
    return ROW.findall(text[at:]) if at >= 0 else []


def screen_hash(rows):
    return hashlib.md5(json.dumps([[r, lab] for _, r, lab in rows[:40]]).encode()).hexdigest()[:12] if rows else None


def screen_title(rows):
    for _, role, label in rows:
        if role == "NavigationBar" and label:
            return label
    for _, role, label in rows[:6]:
        if role in ("StaticText", "Other") and label:
            return label
    return None


def read_jsonl(path):
    out = []
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def turn_from_prompt(prompt, folder=None):
    text = prompt.get("text") or ""
    rows = screen_rows(text)
    labels = {eid: label for eid, _, label in rows}
    match = CURRENT_APP.search(text)
    app = match.group(1).strip() if match else None
    out = prompt.get("out") or {}
    planned = []
    for a in out.get("actions") or ():
        op = a.get("operation") or a.get("op") or ""
        target = a.get("target")
        label = a.get("target_label") or labels.get(target or "", "") or ""
        if op == "LAUNCH_APP":
            label = a.get("app") or a.get("text") or label
        planned.append(Action(op=op, label=label, target=target, text=a.get("text"),
                              app=(a.get("app") if op == "LAUNCH_APP" else app)))
    image = prompt.get("image")
    return Turn(step=prompt.get("step"), app=app, screen=screen_hash(rows), title=screen_title(rows), rows=len(rows),
                planned=planned, notes_add=len(out.get("notes_add") or ()),
                checklist=[c for c in out.get("checklist_updates") or () if isinstance(c, dict)],
                answer=out.get("answer"), thought=out.get("thought"),
                out_chars=len(json.dumps(out, ensure_ascii=False)) if out else None,
                image=str(pathlib.Path(folder) / image) if (folder and image) else image, has_prompt=True)


def attach_events(turns, events):
    """Fold a run's frontier_* events into its turns (by step). Returns the turns, in step order."""
    by_step = {t.step: t for t in turns}
    executed = {}
    for e in events:
        kind, step = e.get("event"), e.get("step")
        if not str(kind).startswith("frontier_") or step is None:
            continue
        turn = by_step.get(step)
        if turn is None:
            turn = by_step[step] = Turn(step=step)
        if kind == "frontier_decision":
            turn.timed = True
            turn.route = e.get("source") or turn.route
            if e.get("latency_ms") is not None:
                turn.model_ms = (turn.model_ms or 0.0) + float(e["latency_ms"])
            if e.get("prep_ms") is not None:
                turn.prep_ms = (turn.prep_ms or 0.0) + float(e["prep_ms"])
            tokens = e.get("tokens") or e.get("usage") or {}
            if isinstance(tokens, dict):
                turn.in_tokens = tokens.get("in", tokens.get("prompt_tokens", turn.in_tokens))
                turn.out_tokens = tokens.get("out", tokens.get("completion_tokens", turn.out_tokens))
                turn.cached_tokens = tokens.get("cached", tokens.get("cached_tokens", turn.cached_tokens))
                turn.write_tokens = tokens.get("write", tokens.get("cache_write_tokens", turn.write_tokens))
                if tokens.get("ttft_ms") is not None:
                    turn.ttft_ms = float(tokens["ttft_ms"])
                    if tokens.get("total_ms") is not None:
                        turn.gen_ms = float(tokens["total_ms"]) - turn.ttft_ms
            if not turn.planned and e.get("ops"):
                turn.planned = [Action(op=op, app=turn.app) for op in e["ops"]]
        elif kind == "frontier_action":
            turn.timed = True
            op = e.get("operation") or ""
            label = e.get("target_label") or ""
            app = e.get("app_name") or turn.app
            text = e.get("text") if isinstance(e.get("text"), str) else None
            index = e.get("index")
            if op == "LAUNCH_APP":
                planned = turn.planned[index] if isinstance(index, int) and index < len(turn.planned) else None
                label = label or (planned.label if planned else "")
                app = (planned.app if planned else None) or label or app
            executed.setdefault(step, []).append(Action(
                op=op, label=label, target=None, text=text, app=app, changed=e.get("changed"),
                refresh_ms=e.get("refresh_ms"), exec_ms=e.get("exec_ms"), settle_ms=e.get("settle_ms"), executed=True))
        elif kind == "frontier_refused":
            turn.refused.append(e.get("label") or "")
        elif kind == "frontier_failed":
            turn.failed.append({"op": e.get("operation"), "error": str(e.get("error") or e.get("detail") or "")[:160]})
        elif kind == "frontier_chunk_stop":
            turn.stops.append(e.get("reason") or "")
    for step, actions in executed.items():
        turn = by_step[step]
        for a, p in zip(actions, turn.planned):
            if not a.label and p.label:
                a.label = p.label
            if a.text is None and p.text is not None and a.op in TYPED_OPS:
                a.text = p.text
        turn.actions = actions
    ordered = [by_step[k] for k in sorted(by_step, key=lambda s: (s is None, s))]
    current = None  # runs recorded without prompts: carry the app forward from the last switch
    for turn in ordered:
        if turn.app:
            current = turn.app
        else:
            turn.app = current
        for a in turn.actions or turn.planned:
            if a.app is None:
                a.app = turn.app
            if a.op == "LAUNCH_APP" and (a.label or a.text):
                current = a.label or a.text
    return ordered


def guard_refusals(turn):
    return [r for r in turn.refused if r not in NON_GUARD_REFUSALS]


# -- iOSWorld task folders -----------------------------------------------------------------------------

def _batch_arm(folder):
    """(batch, arm, repeat) from a task folder's place: <batch>/<A1>/<NNN-task> or <batch-a1>/<NNN-task>."""
    run_dir = folder.parent
    m = re.match(r"^([A-Za-z]+)(\d+)$", run_dir.name)
    if m:  # tuning batches: aa1/A1/...
        return run_dir.parent.name, m.group(1), int(m.group(2))
    m = re.match(r"^(.*?)-([a-z])(\d)$", run_dir.name)
    if m:  # ab7-a1, ab6-b2
        return m.group(1), m.group(2).upper(), int(m.group(3))
    m = re.match(r"^(.*?)-r(\d)$", run_dir.name)
    if m:  # model-gpt-6-sol-r1
        return m.group(1), "A", int(m.group(2))
    return run_dir.name, "A", 1


def load_iosworld(folder, clean_since=CLEAN_SINCE):
    folder = pathlib.Path(folder)
    record = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    mobster = record.get("mobster") or {}
    evaluation = record.get("evaluation")
    batch, arm, repeat = _batch_arm(folder)
    prompts_path = folder / "prompts.jsonl"
    try:
        started = os.path.getmtime(prompts_path if prompts_path.exists() else folder / "task.json")
    except OSError:
        started = None
    judged = bool(evaluation) and record.get("status") in ("ok", None)
    events = mobster.get("events") or []
    turns = [turn_from_prompt(p, folder) for p in read_jsonl(prompts_path)]
    turns = attach_events(turns, events)
    calls = mobster.get("model_calls")
    return Run(folder=str(folder), suite="iosworld", batch=batch, arm=arm, repeat=repeat,
               task=record.get("task") or folder.name.partition("-")[2], category=record.get("category"),
               difficulty=record.get("difficulty"), apps=list(record.get("apps") or ()), goal=record.get("goal") or "",
               model=mobster.get("model"), build=mobster.get("build"), device="sim",
               clean=started is None or started >= clean_since, judged=judged,
               passed=official_pass(evaluation) if judged else None,
               rubric=rubric_score(evaluation) if judged else None,
               criteria=[{"criterion": r.get("criterion"), "satisfied": r.get("satisfied")}
                         for r in (evaluation or {}).get("rubric_results") or ()],
               status=mobster.get("status"), reason=str(mobster.get("reason") or ""),
               answer=record.get("agent_answer") or "", agent_s=mobster.get("agent_seconds"),
               usd=mobster.get("cost_usd"), calls=calls if isinstance(calls, int) else len(turns),
               timing=mobster.get("timing") or {}, events_capped=len(events) >= EVENTS_CAP, started=started,
               turns=turns)


# -- phone and simulator benchmark runs ------------------------------------------------------------------

def _artifact_name(record):
    return re.sub(r"[:+]", "_", f"{record.get('repeat', 0)}:{record.get('task')}:{record.get('agent')}")


def load_bench(batch_dir):
    """Runs from one benchmark batch folder (records.jsonl, plan.json, artifacts/)."""
    batch_dir = pathlib.Path(batch_dir)
    try:
        plan = json.loads((batch_dir / "plan.json").read_text())
    except (OSError, ValueError):
        plan = {}
    port = str(plan.get("wda_url") or "").rsplit(":", 1)[-1]
    device = "phone" if port in ("8100", "") and "sim" not in batch_dir.name else "sim"
    runs = []
    for record in read_jsonl(batch_dir / "records.jsonl"):
        folder = batch_dir / "artifacts" / _artifact_name(record)
        if not folder.is_dir():
            continue
        verdict = record.get("verdict")
        turns = [turn_from_prompt(p, folder) for p in read_jsonl(folder / "prompts.jsonl")]
        events = read_jsonl(folder / "events.jsonl")
        turns = attach_events(turns, events)
        try:
            summary = json.loads((folder / "summary.json").read_text())
        except (OSError, ValueError):
            summary = {}
        config = record.get("config") or {}
        runs.append(Run(folder=str(folder), suite="mobsterbench", batch=batch_dir.name,
                        arm=str(record.get("agent") or "A"), repeat=int(record.get("repeat") or 0) + 1,
                        task=str(record.get("task")), category=record.get("category"), goal="",
                        model=config.get("model") or summary.get("model"), device=device, clean=True,
                        judged=verdict in ("pass", "fail"), passed=(verdict == "pass") if verdict in ("pass", "fail")
                        else None, rubric=None, status=record.get("status"), reason=str(record.get("reason") or ""),
                        answer=json.dumps(record.get("answer")) if record.get("answer") is not None else "",
                        agent_s=(record.get("agent_ms") or 0) / 1000 or None, usd=record.get("cost_usd"),
                        calls=record.get("model_calls"), timing=summary.get("timing") or {},
                        events_capped=False, started=None, turns=turns))
    return runs


# -- discovery ---------------------------------------------------------------------------------------------

def discover(roots, clean_since=CLEAN_SINCE, suites=("iosworld", "mobsterbench")):
    """Every run under ``roots`` (batch folders or folders of them), each loaded once."""
    runs, seen = [], set()
    for root in roots:
        root = pathlib.Path(root).expanduser()
        if "iosworld" in suites:
            pattern = [str(root / "task.json"), str(root / "*" / "task.json"), str(root / "*" / "*" / "task.json"),
                       str(root / "*" / "*" / "*" / "task.json")]
            for task_json in sorted({p for pat in pattern for p in glob.glob(pat)}):
                folder = pathlib.Path(task_json).parent.resolve()
                if folder in seen or not re.match(r"^\d+-", folder.name):
                    continue
                seen.add(folder)
                try:
                    runs.append(load_iosworld(folder, clean_since))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        if "mobsterbench" in suites:
            for records in sorted(set(glob.glob(str(root / "records.jsonl")) + glob.glob(str(root / "*" / "records.jsonl")))):
                batch_dir = pathlib.Path(records).parent.resolve()
                if batch_dir in seen:
                    continue
                seen.add(batch_dir)
                runs.extend(load_bench(batch_dir))
    return runs

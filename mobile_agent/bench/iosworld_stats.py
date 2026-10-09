"""iOSWorld scoring, paired A/B comparison and pooled full-mix estimates, from recorded runs.

$0, offline, seconds: it reads the run folders ``bench/iosworld.py`` writes (``task.json``,
``trajectory.json``, ``prompts.jsonl``) and the worker logs beside them. It calls no model
and no simulator.

**The pass rule is iOSWorld's own.** ``scripts/judge_trajectories.py`` ``_aggregate`` counts a
task as passed only when every rubric criterion is satisfied, and falls back to the judge's
``success`` flag only when the evaluation has no rubric results. The judge's ``success`` flag is
its own overall opinion, and it disagreed with the rule on 4 of the 32 round-2 A/B runs, so it
is shown beside the official verdict, never instead of it.

    python -m mobile_agent.bench.iosworld report --run research/iosworld-runs/NAME
    python -m mobile_agent.bench.iosworld compare --a runs/ab6-a1 runs/ab6-a2 --b runs/ab6-b1 runs/ab6-b2
    python -m mobile_agent.bench.iosworld estimate research/iosworld-runs/* --model gpt-5.6-sol

**Mechanism counts** (the research's primary A/B metrics, section 3 of the 26 Sep report),
per run:

- ``guard_refusals``: taps the commit guard refused (``frontier_refused`` events whose label is
  a control, not loop/repeat/idle_wait/dismiss_exhausted). Exact.
- ``required_refusals`` / ``unrequested_commits``: a guard refusal the task asked for, and an
  executed commit-class TAP or LONG_PRESS it did not ask for. **Approximate**: "asked for" means
  the task's goal or its rubric criteria use the control's act or a synonym (``commit_requested``).
  It is lenient (a noun such as "my recent order" counts as asking for an order), so required
  refusals are an upper bound and unrequested commits a lower bound. A labels file
  (``--commit-labels``) overrides it per (task, label). Commits sent with Return (SUBMIT,
  TYPE_SUBMIT) are not seen.
- ``done_deferrals``: turns whose DONE the code deferred (``verify_before_done``, or the proof gate's
  ``proof_gate``: an item not yet shown on screen). Exact.
- ``false_claims``: runs that ended DONE ("completed") yet failed the official rule.
  **Approximate, an upper bound**: it also counts judge misses (the work was done but the frames
  did not show it) and wrong answers. Telling these apart needs a human reading the frames.
- ``wda_crash``: the run ended in ``error`` on a WDA transport failure (connection refused or
  reset, a dropped HTTP connection). Exact for the recorded reason.
- ``calls``: model calls (``task.json`` ``model_calls``; else prompts.jsonl lines).
- ``revisits``: turns back on a screen already seen earlier in the run, other than the turn just
  before (circling). ``stays`` counts turns on the same screen as the turn before; the research's
  ``revisit.py`` counted both together. **Approximate**: a screen is the ordered role+label list of
  its first 40 elements in the turn's prompt, so values (typed text, a toggled switch) are ignored.

Worker logs add what a run folder cannot show: tasks the harness never ran ("NOT RUN", a failed
reset) and workers that died with a traceback.
"""

import collections
import dataclasses
import glob
import hashlib
import json
import math
import os
import pathlib
import random
import re
import statistics

from ..task_policy import COMMIT_CONTROL

# iOSWorld's 133 tasks by category (tasks.json, 26 Sep 2026).
FULL_MIX = {"single_app": 27, "multi_app": 60, "memory": 46}
CATEGORIES = tuple(FULL_MIX)
# frontier_refused labels that are not the commit guard.
NON_GUARD_REFUSALS = frozenset({"loop", "repeat", "idle_wait", "dismiss_exhausted"})
COMMIT_OPS = frozenset({"TAP", "LONG_PRESS"})
SHEET_OPENERS = frozenset({"share", "share…", "share..."})  # as frontier.guard: opening a sheet commits nothing
GUARD_WORDS = 4
EVENTS_CAP = 400  # iosworld.run_task keeps the last 400 events: counts from a capped list may be short
WDA_CRASH = re.compile(r"ConnectionRefused|ConnectionReset|ConnectionAborted|RemoteDisconnected|BrokenPipe|"
                       r"TransportError|URLError|IncompleteRead|Connection refused|Connection reset", re.I)
SCREEN_ROW = re.compile(r'^e\d+ (\w+) "([^"]*)"', re.M)
SEED = 20260926
BOOTSTRAP = 10000

# A commit-class control's act -> the words in a request that ask for it. Inflections are matched
# by ``_says``. "confirm" is special: it completes whatever other commit the request asks for.
_SEND = {"send", "sent", "message", "messaged", "reply", "text", "email", "mail", "dm", "invite", "request",
         "tell", "notify", "forward", "share", "post"}
_POST = {"post", "comment", "publish", "tweet", "reply", "share", "react", "message"}
_PAY = {"pay", "paid", "payment", "checkout", "check out", "order", "buy", "bought", "purchase", "settle",
        "charge", "transfer", "send"}
_BOOK = {"book", "reserve", "reservation", "booking"}
_FOLLOW = {"follow", "subscribe", "join", "connect"}
_DELETE = {"delete", "erase", "remove", "clear", "discard", "trash", "unfollow"}
_CALL = {"call", "dial", "phone", "facetime", "ring"}
_SUBMIT = {"submit", "apply", "register", "sign up", "signup", "enroll", "upload", "install", "redeem"}
ACT_WORDS = {
    "send": _SEND, "reply": _SEND | _POST, "share": _SEND | _POST,
    "post": _POST, "comment": _POST, "publish": _POST, "tweet": _POST,
    "pay": _PAY, "checkout": _PAY, "order": _PAY, "place order": _PAY, "buy": _PAY, "purchase": _PAY,
    "transfer": _PAY, "donate": _PAY | {"donate", "donation"}, "tip": _PAY | {"tip"},
    "book": _BOOK, "reserve": _BOOK,
    "follow": _FOLLOW, "subscribe": _FOLLOW,
    "delete": _DELETE, "erase": _DELETE, "remove": _DELETE, "reset": {"reset", "erase", "restore"},
    "call": _CALL, "dial": _CALL, "facetime": _CALL,
    "install": _SUBMIT, "upload": _SUBMIT, "redeem": _SUBMIT, "submit": _SUBMIT, "sign up": _SUBMIT,
    "signup": _SUBMIT,
}
# Any of these in a request makes a bare "Confirm" control part of a requested commit.
_CONFIRMABLE = _SEND | _POST | _PAY | _BOOK | _FOLLOW | _SUBMIT


# -- the official verdict -------------------------------------------------------------------

def official_pass(evaluation):
    """iOSWorld's pass rule (judge_trajectories.py ``_aggregate``): every rubric criterion
    satisfied; the judge's ``success`` only when there are no rubric results at all."""
    evaluation = evaluation or {}
    results = evaluation.get("rubric_results")
    if results is not None:
        return bool(results) and all(r.get("satisfied") for r in results)
    return bool(evaluation.get("success"))


def rubric_score(evaluation):
    """The judge's rubric score, as ``_aggregate`` averages it (missing counts as 0)."""
    return float((evaluation or {}).get("score") or 0.0)


# -- commits: which the task asks for --------------------------------------------------------

def _words(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return re.findall(r"[a-z]+", text.casefold())


def _says(words, joined, phrase):
    """Whether ``phrase`` (or an inflection: -s, -es, -ed, -d, -ing, a doubled consonant) is said."""
    if " " in phrase:
        return f" {phrase} " in joined
    stem = re.escape(phrase)
    pattern = re.compile(rf"{stem}(s|es|ed|d|ing|{stem[-1]}ed|{stem[-1]}ing)?")
    return any(pattern.fullmatch(word) for word in words)


def commit_acts(label):
    """The commit acts a control's label names, read as the guard reads it (its first words), with
    identifiers split ("mybank.checkout.confirmToggle" -> checkout, confirm)."""
    words = _words(label)[:GUARD_WORDS]
    text = " ".join(words)
    if text in SHEET_OPENERS:
        return set()
    return {m.group(0).casefold() for m in COMMIT_CONTROL.finditer(text)}


def commit_requested(label, request_text, overrides=None, task=None):
    """True when the request (goal plus rubric criteria) asks for the act ``label`` commits, False when
    it does not, None when ``label`` names no commit. A hand label in ``overrides`` wins:
    {task: {label: true|false}}. Approximate: see the module docstring."""
    if overrides and task in overrides and label in overrides[task]:
        return bool(overrides[task][label])
    acts = commit_acts(label)
    if not acts:
        return None
    words = _words(request_text)
    joined = " " + " ".join(words) + " "
    others = acts - {"confirm"}
    if not others:
        return any(_says(words, joined, w) for w in _CONFIRMABLE)
    return all(any(_says(words, joined, w) for w in ACT_WORDS.get(act, {act})) for act in others)


# -- one run of one task ---------------------------------------------------------------------

@dataclasses.dataclass
class TaskRun:
    run: str
    folder: str
    task: str
    category: str = None
    difficulty: str = None
    apps: int = 0
    judged: bool = False
    official: bool = False
    success: bool = False
    score: float = 0.0
    status: str = None
    reason: str = ""
    model: str = None
    calls: int = 0
    agent_seconds: float = None
    cost_usd: float = None
    guard_refusals: list = dataclasses.field(default_factory=list)
    required_refusals: int = 0
    commits: list = dataclasses.field(default_factory=list)
    unrequested_commits: int = 0
    done_deferrals: int = 0
    false_claim: bool = False
    wda_crash: bool = False
    turns: int = 0
    revisits: int = 0
    stays: int = 0
    events_capped: bool = False

    @property
    def disagrees(self):
        """The judge's ``success`` flag and the official rule differ."""
        return self.judged and self.official != self.success


def _screen_key(text):
    at = (text or "").find("Screen elements:")
    rows = SCREEN_ROW.findall(text[at:]) if at >= 0 else []
    return hashlib.md5(json.dumps(rows[:40]).encode()).hexdigest() if rows else None


def read_prompts(path):
    """(turns, revisits, stays) from a run's prompts.jsonl: a revisit is a turn back on a screen seen
    earlier in the run but not on the turn just before (circling); a stay is a turn on the same
    screen as the turn before (after a no-change action, or typing that changed only values)."""
    turns = revisits = stays = 0
    seen, previous = set(), None
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0, 0
    for line in lines:
        try:
            prompt = json.loads(line)
        except ValueError:
            continue
        turns += 1
        key = _screen_key(prompt.get("text"))
        if key is not None:
            stays += key == previous
            revisits += key in seen and key != previous
            seen.add(key)
        previous = key
    return turns, revisits, stays


def load_task(task_json, overrides=None):
    """A TaskRun from one task folder's task.json (and prompts.jsonl beside it)."""
    folder = os.path.dirname(task_json)
    record = json.loads(pathlib.Path(task_json).read_text(encoding="utf-8"))
    mobster = record.get("mobster") or {}
    evaluation = record.get("evaluation")
    run = TaskRun(run=os.path.basename(os.path.dirname(folder)), folder=folder,
                  task=record.get("task") or os.path.basename(folder).partition("-")[2],
                  category=record.get("category"), difficulty=record.get("difficulty"),
                  apps=len(record.get("apps") or ()),
                  judged=bool(evaluation) and record.get("status") in ("ok", None),
                  status=mobster.get("status"), reason=str(mobster.get("reason") or ""),
                  model=mobster.get("model"), agent_seconds=mobster.get("agent_seconds"),
                  cost_usd=mobster.get("cost_usd"))
    if run.judged:
        run.official, run.success = official_pass(evaluation), bool(evaluation.get("success"))
        run.score = rubric_score(evaluation)
    criteria = " ".join(r.get("criterion", "") for r in (evaluation or {}).get("rubric_results") or ())
    request = f"{record.get('goal') or ''} {criteria}"
    events = mobster.get("events") or []
    run.events_capped = len(events) >= EVENTS_CAP
    for event in events:
        kind = event.get("event")
        if kind == "frontier_refused" and event.get("label") not in NON_GUARD_REFUSALS:
            label = event.get("label") or ""
            run.guard_refusals.append(label)
            run.required_refusals += commit_requested(label, request, overrides, run.task) is True
        elif kind == "frontier_action" and event.get("operation") in COMMIT_OPS:
            label = event.get("target_label") or ""
            asked = commit_requested(label, request, overrides, run.task)
            if asked is not None:
                run.commits.append(label)
                run.unrequested_commits += asked is False
        elif kind == "frontier_chunk_stop" and event.get("reason") in ("verify_before_done", "proof_gate"):
            run.done_deferrals += 1
    run.turns, run.revisits, run.stays = read_prompts(os.path.join(folder, "prompts.jsonl"))
    calls = mobster.get("model_calls")
    run.calls = calls if isinstance(calls, int) else run.turns
    run.false_claim = run.judged and run.status == "completed" and not run.official
    run.wda_crash = run.status == "error" and bool(WDA_CRASH.search(run.reason))
    return run


_STATUS_LINE = re.compile(r"^(\S+)\s+NOT RUN\b")


def read_worker_logs(run_dir):
    """{"not_run": [task...], "tracebacks": n} from ``<run>.worker*.log`` beside a run folder."""
    run_dir = pathlib.Path(run_dir)
    not_run, tracebacks = [], 0
    for log in sorted(glob.glob(str(run_dir.parent / f"{glob.escape(run_dir.name)}.worker*.log"))):
        try:
            text = pathlib.Path(log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        not_run += [m.group(1) for m in map(_STATUS_LINE.match, text.splitlines()) if m]
        tracebacks += text.count("Traceback (most recent call last)")
    return {"not_run": not_run, "tracebacks": tracebacks}


def load_runs(paths, model=None, overrides=None):
    """TaskRuns from run folders (each holding NNN-task/task.json), optionally one model's only."""
    runs = []
    for path in paths:
        for task_json in sorted(glob.glob(os.path.join(glob.escape(str(path)), "*", "task.json"))):
            try:
                run = load_task(task_json, overrides)
            except (OSError, ValueError, KeyError):
                continue
            if model and run.model != model:
                continue
            runs.append(run)
    return runs


def load_overrides(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8")) if path else None


# -- statistics ------------------------------------------------------------------------------

def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def se(xs):
    """Standard error of the mean of ``xs``."""
    xs = list(xs)
    return statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None


def percentile(sorted_xs, q):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def bootstrap_mean_ci(values, n=BOOTSTRAP, seed=SEED, level=.95):
    """Percentile CI of the mean of ``values`` (one value per task: the resampled unit is a task)."""
    values = list(values)
    if not values:
        return None, None
    rng = random.Random(seed)
    k = len(values)
    means = sorted(sum(rng.choices(values, k=k)) / k for _ in range(n))
    return percentile(means, (1 - level) / 2), percentile(means, 1 - (1 - level) / 2)


def by_task(runs):
    grouped = collections.defaultdict(list)
    for run in runs:
        grouped[run.task].append(run)
    return grouped


# -- paired A/B ------------------------------------------------------------------------------

MECHANISMS = (
    # (key, label, per-run value, approximate?)
    ("required_refusals", "refusals of required commits", lambda r: r.required_refusals, True),
    ("guard_refusals", "  all guard refusals", lambda r: len(r.guard_refusals), False),
    ("unrequested_commits", "unrequested commits (must be 0)", lambda r: r.unrequested_commits, True),
    ("done_deferrals", "DONE-deferral turns", lambda r: r.done_deferrals, False),
    ("false_claims", "false completion claims (upper bound)", lambda r: int(r.false_claim), True),
    ("wda_crashes", "runs ended by a WDA crash", lambda r: int(r.wda_crash), False),
    ("calls", "calls per task", lambda r: r.calls, False),
    ("revisits", "revisit turns (back to an earlier screen)", lambda r: r.revisits, True),
    ("stays", "  same-screen turns", lambda r: r.stays, True),
)


def arm_summary(runs):
    judged = [r for r in runs if r.judged]
    tasks = by_task(judged)
    per_task_score = [mean(r.score for r in rs) for rs in tasks.values()]
    agreeing = sum(len({r.official for r in rs}) == 1 for rs in tasks.values() if len(rs) > 1)
    repeated = sum(len(rs) > 1 for rs in tasks.values())
    mechanisms = {}
    for key, _, value, _ in MECHANISMS:
        values = [value(r) for r in runs]
        mechanisms[key] = {"total": sum(values), "per_run": mean(values)}
    return {"runs": len(runs), "judged": len(judged), "tasks": len(tasks),
            "official": sum(r.official for r in judged), "success": sum(r.success for r in judged),
            "disagreements": [(r.run, r.task, r.official, r.success) for r in judged if r.disagrees],
            "rubric_mean": mean(r.score for r in judged),
            "rubric_se": se(per_task_score),  # clustered by task: the runs of a task are not independent
            "agreeing_tasks": agreeing, "repeated_tasks": repeated,
            "events_capped": sum(r.events_capped for r in runs), "mechanisms": mechanisms}


def compare(a_runs, b_runs, n_boot=BOOTSTRAP, seed=SEED):
    """Paired comparison of arm B against arm A on the tasks both judged."""
    a_tasks = by_task(r for r in a_runs if r.judged)
    b_tasks = by_task(r for r in b_runs if r.judged)
    paired = sorted(set(a_tasks) & set(b_tasks))
    rows = []
    for task in sorted(set(a_tasks) | set(b_tasks)):
        row = {"task": task, "paired": task in paired}
        for arm, tasks in (("a", a_tasks), ("b", b_tasks)):
            rs = tasks.get(task, [])
            row[arm] = {"runs": len(rs), "official": sum(r.official for r in rs), "success": sum(r.success for r in rs),
                        "rubric": mean(r.score for r in rs), "calls": mean(r.calls for r in rs),
                        "required_refusals": sum(r.required_refusals for r in rs),
                        "unrequested_commits": sum(r.unrequested_commits for r in rs),
                        "done_deferrals": sum(r.done_deferrals for r in rs),
                        "false_claims": sum(r.false_claim for r in rs), "wda_crashes": sum(r.wda_crash for r in rs),
                        "revisits": sum(r.revisits for r in rs)}
        rows.append(row)
    pass_diff = [mean(r.official for r in b_tasks[t]) - mean(r.official for r in a_tasks[t]) for t in paired]
    score_diff = [mean(r.score for r in b_tasks[t]) - mean(r.score for r in a_tasks[t]) for t in paired]
    result = {"paired_tasks": len(paired), "rows": rows,
              "a": arm_summary(a_runs), "b": arm_summary(b_runs),
              "pass_diff": mean(pass_diff), "pass_diff_se": se(pass_diff),
              "pass_diff_ci": bootstrap_mean_ci(pass_diff, n_boot, seed),
              "rubric_diff": mean(score_diff), "rubric_diff_se": se(score_diff),
              "rubric_diff_ci": bootstrap_mean_ci(score_diff, n_boot, seed)}
    return result


def _pct(x):
    return "-" if x is None else f"{100 * x:+.1f}"


def _fmt(x, digits=3):
    return "-" if x is None else f"{x:.{digits}f}"


def format_compare(result, a_name="A", b_name="B", logs=None):
    lines = []
    a, b = result["a"], result["b"]
    width = max(len(r["task"]) for r in result["rows"]) if result["rows"] else 8
    lines.append(f"{'task':{width}}  {a_name + ' pass':>8} {a_name + ' rubric':>9} {a_name + ' calls':>8}  "
                 f"{b_name + ' pass':>8} {b_name + ' rubric':>9} {b_name + ' calls':>8}  mechanisms A | B")
    for row in result["rows"]:
        cells = []
        for arm in ("a", "b"):
            x = row[arm]
            cells.append(f"{x['official']}/{x['runs']:<3}".rjust(8) + f" {_fmt(x['rubric'], 2):>9} {_fmt(x['calls'], 1):>8}")
        marks = []
        for arm in ("a", "b"):
            x = row[arm]
            tags = [f"{n}{x[k]}" for k, n in (("required_refusals", "R"), ("unrequested_commits", "U"),
                                              ("done_deferrals", "D"), ("false_claims", "F"),
                                              ("wda_crashes", "W")) if x.get(k)]
            marks.append(" ".join(tags) or ".")
        lines.append(f"{row['task']:{width}}  {cells[0]}  {cells[1]}  {marks[0]} | {marks[1]}"
                     + ("" if row["paired"] else "  (unpaired)"))
    lines.append("  mechanisms: R required refusals, U unrequested commits, D DONE deferrals, "
                 "F false completion claims (upper bound), W WDA crash")
    lines.append("")

    def row(label, x, y, extra=""):
        lines.append(f"{label:44} {x:>14} {y:>14}" + (f"   {extra}" if extra else ""))

    row("", a_name, b_name)
    row("official passes (every criterion)", f"{a['official']}/{a['judged']}", f"{b['official']}/{b['judged']}")
    row("judge success flag", f"{a['success']}/{a['judged']}", f"{b['success']}/{b['judged']}")
    row("mean rubric score (SE by task)", f"{_fmt(a['rubric_mean'])} ({_fmt(a['rubric_se'])})",
        f"{_fmt(b['rubric_mean'])} ({_fmt(b['rubric_se'])})")
    if a["repeated_tasks"] or b["repeated_tasks"]:
        row("same-build agreement (tasks)", f"{a['agreeing_tasks']}/{a['repeated_tasks']}",
            f"{b['agreeing_tasks']}/{b['repeated_tasks']}")
    for key, label, _, approximate in MECHANISMS:
        ma, mb = a["mechanisms"][key], b["mechanisms"][key]
        label += " ~" if approximate else ""
        if key == "calls":
            row(label, _fmt(ma["per_run"], 1), _fmt(mb["per_run"], 1))
        else:
            row(label, ma["total"], mb["total"], f"per run {_fmt(ma['per_run'], 2)} | {_fmt(mb['per_run'], 2)}")
    if logs:
        for arm, name in (("a", a_name), ("b", b_name)):
            info = logs.get(arm) or {}
            if info.get("not_run") or info.get("tracebacks"):
                lines.append(f"{name}: worker logs: not run {len(info['not_run'])} {info['not_run'][:6]}, "
                             f"tracebacks {info['tracebacks']}")
    lines.append("  ~ approximate (see mobile_agent/bench/iosworld_stats.py)")
    lines.append("")
    lo, hi = result["pass_diff_ci"]
    lines.append(f"paired over {result['paired_tasks']} tasks, {b_name} - {a_name}:")
    lines.append(f"  pass rate    {_pct(result['pass_diff'])} points, SE {_fmt(result['pass_diff_se'] and 100 * result['pass_diff_se'], 1)}, "
                 f"95% bootstrap CI [{_pct(lo)}, {_pct(hi)}]")
    lo, hi = result["rubric_diff_ci"]
    lines.append(f"  rubric score {result['rubric_diff']:+.3f}, SE {_fmt(result['rubric_diff_se'])}, "
                 f"95% bootstrap CI [{lo:+.3f}, {hi:+.3f}]" if result["rubric_diff"] is not None else "  rubric score -")
    for arm, name in (("a", a_name), ("b", b_name)):
        if result[arm]["disagreements"]:
            lines.append(f"{name}: judge success flag disagrees with the official rule on "
                         + ", ".join(f"{run}/{task} (official {'pass' if o else 'fail'})"
                                     for run, task, o, _ in result[arm]["disagreements"]))
        if result[arm]["events_capped"]:
            lines.append(f"{name}: {result[arm]['events_capped']} runs hit the {EVENTS_CAP}-event cap: counts may be short")
    return "\n".join(lines)


# -- pooled full-mix estimate ----------------------------------------------------------------

def _category_rate(tasks, weighting):
    if weighting == "run":
        runs = [r for rs in tasks.values() for r in rs]
        return mean(r.official for r in runs)
    return mean(mean(r.official for r in rs) for rs in tasks.values())


def _mix_estimate(groups, mix, weighting):
    covered = {c: w for c, w in mix.items() if groups.get(c)}
    total = sum(covered.values())
    return sum(w * _category_rate(groups[c], weighting) for c, w in covered.items()) / total if total else None


def estimate(runs, mix=None, weighting="task", n_boot=BOOTSTRAP, seed=SEED, level=.95):
    """The official pass rate reweighted to iOSWorld's category mix, with a stratified cluster
    bootstrap CI (tasks resampled within each category, all runs of a drawn task kept).

    ``weighting="task"`` averages each task's pass rate (the unbiased estimate of the mix when
    tasks have unequal run counts); ``"run"`` pools runs, as the 26 Sep report's 58% did."""
    mix = dict(mix or FULL_MIX)
    judged = [r for r in runs if r.judged]
    groups = {c: dict(by_task(r for r in judged if r.category == c)) for c in mix}
    point = _mix_estimate(groups, mix, weighting)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        sample = {}
        for c, tasks in groups.items():
            names = list(tasks)
            sample[c] = {i: tasks[t] for i, t in enumerate(rng.choices(names, k=len(names)))} if names else {}
        draws.append(_mix_estimate(sample, mix, weighting))
    draws = sorted(d for d in draws if d is not None)
    categories = {}
    for c in mix:
        tasks = groups[c]
        runs_c = [r for rs in tasks.values() for r in rs]
        categories[c] = {"tasks": len(tasks), "runs": len(runs_c), "passes": sum(r.official for r in runs_c),
                         "rate": _category_rate(tasks, weighting) if tasks else None}
    missing = [c for c in mix if not groups[c]]
    return {"estimate": point, "ci": (percentile(draws, (1 - level) / 2), percentile(draws, 1 - (1 - level) / 2)),
            "se": statistics.stdev(draws) if len(draws) > 1 else None, "weighting": weighting,
            "runs": len(judged), "tasks": len({r.task for r in judged}), "passes": sum(r.official for r in judged),
            "categories": categories, "mix": mix, "missing": missing}


def format_estimate(result):
    mix = result["mix"]
    lines = [f"{result['runs']} judged runs over {result['tasks']} tasks, {result['passes']} official passes "
             f"({result['weighting']}-weighted within category)"]
    for c, info in result["categories"].items():
        rate = "-" if info["rate"] is None else f"{100 * info['rate']:.1f}%"
        lines.append(f"  {c:11} {info['passes']:3}/{info['runs']:<3} runs over {info['tasks']:2} tasks  {rate:>6}  "
                     f"(weight {mix[c]}/{sum(mix.values())})")
    lo, hi = result["ci"]
    if result["estimate"] is None:
        lines.append("no judged runs")
    else:
        lines.append(f"full-mix estimate {100 * result['estimate']:.1f}%, 95% CI {100 * lo:.1f}-{100 * hi:.1f}% "
                     f"(SE {100 * result['se']:.1f} points; tasks resampled within category)")
    if result["missing"]:
        lines.append(f"  no runs in {', '.join(result['missing'])}: the estimate covers the other categories only")
    return "\n".join(lines)


# -- one run folder's report -----------------------------------------------------------------

def report_lines(runs):
    """The ``iosworld report`` summary: official passes first, the judge's flag where it disagrees."""
    judged = [r for r in runs if r.judged]
    official = sum(r.official for r in judged)
    lines = [f"tasks run {len(runs)}, judged {len(judged)}, official passes {official}"
             + (f" ({100 * official / len(judged):.1f}%)" if judged else "")
             + " (every rubric criterion satisfied: iOSWorld's rule)"]
    if judged:
        lines.append(f"mean rubric score {mean(r.score for r in judged):.3f}")
        flag = sum(r.success for r in judged)
        disagree = [r for r in judged if r.disagrees]
        lines.append(f"judge success flag {flag}/{len(judged)}; disagrees with the official rule on {len(disagree)}"
                     + (": " + ", ".join(f"{r.task} (official {'pass' if r.official else 'fail'}, "
                                         f"flag {'pass' if r.success else 'fail'}, score {r.score:.2f})"
                                         for r in disagree) if disagree else ""))
    return lines

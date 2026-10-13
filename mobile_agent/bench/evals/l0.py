"""L0: trace analytics on runs we already have. $0, seconds, no device, no model.

    python -m mobile_agent.bench.evals.l0 --runs DIR... [--batches aa1,ac1] [--app-groups groups.json] \\
        [--labels labels.jsonl] --out OUT_DIR

Mines every recorded run (``traces.discover``) for four things and writes one ranked table:

1. **Wasted actions** (per turn, so nothing is counted twice within a mechanism):
   - ``round_trip``: a turn opened a screen and the next turn left it for the screen before, with nothing written down
     on the way (no note, no checklist item found or done, no answer).
   - ``repeat_action``: the same action on the same screen as earlier in the run, with nothing gained in between.
   - ``no_change``: an action the driver ran that changed nothing (typing excepted).
   - ``guard_refusal`` and ``loop_refusal``: the code refused the turn's action (commit guard; loop, repeat, idle).
   - ``failed_action``: the driver raised.
   - ``done_deferred``: a DONE the code sent back (proof gate, verify before done).
   - ``chainable_turn``: a turn on the same screen as the turn before, whose target was already on that screen and
     whose turn before ran cleanly: one more action the previous turn could have chained.
   - ``done_only_final``: the run's last turn has only DONE: the fixed ~2 s finishing call.
2. **Step inefficiency**: a passing run's turns and actions beyond the fewest any passing run of the same task took.
3. **Time sinks** by step type (ground, type, scroll, switch, end) and by app group: model, generation, exec, settle,
   refresh. Settle after the screen stopped moving comes from ``frameclock.jsonl`` where a run has it.
4. **First divergence** of each failing run from the closest passing run of the same task: actions are aligned by
   their longest common subsequence; the divergence is where the failing run leaves the passing path for good, and
   its kind (stopped early, a different app or order, a different element, scrolled instead...).

The mechanisms overlap across rows (a refused repeat is both): each row is a ceiling on what removing that one
mechanism could save, not a share of a partition.
"""

import argparse
import collections
import json
import pathlib
import statistics

from . import traces as T

# Which hypothesis (the tuning plan's ranked list) each mechanism would feed.
HYPOTHESES = {
    "round_trip": "H2 stall recovery, H5 screen-diff history",
    "repeat_action": "H2 stall recovery",
    "loop_refusal": "H2 stall recovery",
    "no_change": "H10 device time, grounding",
    "guard_refusal": "commit guard (#224)",
    "failed_action": "infra, H10 device time",
    "done_deferred": "H3 completion check, contract quality",
    "chainable_turn": "H7 token diet, H9 speculative call (chain more)",
    "done_only_final": "H8 DONE folded into the last action",
    "excess_turns": "H1 typed slots, H2, H5 (shorter paths)",
    "settle_after_still": "H10 settle on frame stability",
    "typing_exec": "H10 device time (typing)",
    "source_reads": "H4 fewer device reads (overlaps settle, refresh, exec)",
    "repeated_reads": "H4 fewer device reads",
    "generation": "H7 token diet, H6 streaming dispatch",
    "first_token": "H11 tail control, H6",
}
WASTE = ("round_trip", "repeat_action", "no_change", "guard_refusal", "loop_refusal", "failed_action",
         "done_deferred", "chainable_turn", "done_only_final", "repeated_reads")
LOOP_LABELS = frozenset({"loop", "repeat", "idle_wait", "dismiss_exhausted"})
DEFER_STOPS = frozenset({"proof_gate", "verify_before_done", "done_not_arrived"})


def _labels_on(turn_text_rows):
    return {T.norm(label) for _, _, label in turn_text_rows}


def wasted(run, screens=None):
    """[(mechanism, step, turns, actions, seconds)] for one run. ``screens``: {step: set of labels on that screen}."""
    out, turns = [], run.turns
    screens = screens or {}
    seen = {}
    gained_since = 0
    for i, t in enumerate(turns):
        executed = [a for a in t.actions if a.executed]
        model_s = ((t.model_ms or 0.0) + (t.prep_ms or 0.0)) / 1000
        nothing_ran = not executed
        guards = T.guard_refusals(t)
        loops = [r for r in t.refused if r in LOOP_LABELS]
        if guards:
            out.append(("guard_refusal", t.step, 1 if nothing_ran else 0, len(guards), model_s if nothing_ran else 0.0))
        if loops:
            out.append(("loop_refusal", t.step, 1 if nothing_ran else 0, len(loops), model_s if nothing_ran else 0.0))
        if t.failed:
            out.append(("failed_action", t.step, 1 if nothing_ran else 0, len(t.failed),
                        (model_s if nothing_ran else 0.0)))
        if set(t.stops) & DEFER_STOPS and t.done:
            out.append(("done_deferred", t.step, 1, 0, t.seconds))
        dead = [a for a in executed if a.changed is False and a.op not in T.TYPED_OPS and a.op not in ("WAIT", "READ_LIST")
                and a.op not in T.END_OPS]
        if dead:
            whole = len(dead) == len(executed) and not t.gained
            out.append(("no_change", t.step, 1 if whole else 0, len(dead),
                        sum(a.device_ms for a in dead) / 1000 + (model_s if whole else 0.0)))
        # the same action on the same screen as before, nothing gained in between
        for a in executed:
            key = (t.screen, a.token())
            if t.screen and key in seen and seen[key] >= gained_since and not t.gained:
                out.append(("repeat_action", t.step, 1, 1, t.seconds))
                break
        for a in executed:
            seen.setdefault((t.screen, a.token()), i)
        if t.gained:
            gained_since = i
        # A -> B -> A, nothing written down at B, B's way out a back control (or a tap on A's own title)
        if 0 < i < len(turns) - 1:
            a_turn, b_turn, c_turn = turns[i - 1], t, turns[i + 1]
            if (a_turn.screen and b_turn.screen and c_turn.screen == a_turn.screen and b_turn.screen != a_turn.screen
                    and not b_turn.gained and b_turn.actions
                    and all(x.is_back or (a_turn.title and T.norm(x.label) == T.norm(a_turn.title))
                            or x.op in ("SWIPE_RIGHT", "HOME") for x in b_turn.actions)):
                # the opening turn's model call counts only when it wrote nothing down about its own screen
                a_cost = a_turn.device_ms / 1000 if a_turn.gained else a_turn.seconds
                out.append(("round_trip", b_turn.step, 1 + (not a_turn.gained),
                            len(a_turn.actions) + len(b_turn.actions), a_cost + b_turn.seconds))
        # one more action the previous turn could have chained
        if i > 0 and not t.done:
            prev = turns[i - 1]
            labels = screens.get(prev.step)
            if (prev.screen and t.screen == prev.screen and prev.actions and not prev.refused and not prev.failed
                    and all(x.changed is not False or x.op in T.TYPED_OPS for x in prev.actions)
                    and labels is not None and executed and all(T.norm(x.label) in labels for x in executed
                                                                  if x.op in ("TAP", "LONG_PRESS") or x.op in T.TYPED_OPS)
                    and any(x.op in ("TAP", "LONG_PRESS") or x.op in T.TYPED_OPS for x in executed)):
                out.append(("chainable_turn", t.step, 1, 0, model_s))
    if turns and turns[-1].done_only and run.passed:
        last = turns[-1]
        out.append(("done_only_final", last.step, 1, 0, ((last.model_ms or 0.0) + (last.prep_ms or 0.0)) / 1000))
    return out


def screen_labels(run):
    """{step: labels on the screen the model saw} from the prompts (the loader keeps only the hash)."""
    out = {}
    for p in T.read_jsonl(pathlib.Path(run.folder) / "prompts.jsonl"):
        out[p.get("step")] = _labels_on(T.screen_rows(p.get("text") or ""))
    return out


def settle_after_still(run):
    """(seconds, actions) of settle wait after the screen's last motion, from the run's frameclock.jsonl."""
    total, n = 0.0, 0
    for row in T.read_jsonl(pathlib.Path(run.folder) / "frameclock.jsonl"):
        if row.get("kind") != "settle":
            continue
        settle, motion = row.get("settle_ms"), row.get("motion_after_dispatch_ms")
        if isinstance(settle, (int, float)) and isinstance(motion, (int, float)):
            total += max(0.0, settle - max(motion, 0.0))
            n += 1
    return total / 1000, n


# -- step inefficiency -----------------------------------------------------------------------------------

def shortest(runs):
    """{(suite, task): (turns, actions)}: the fewest of any passing run."""
    best = {}
    for r in runs:
        if r.passed and r.turns:
            key = (r.suite, r.task)
            cand = (len(r.turns), len(r.actions))
            if key not in best:
                best[key] = cand
            else:
                best[key] = (min(best[key][0], cand[0]), min(best[key][1], cand[1]))
    return best


def excess(run, best):
    b = best.get((run.suite, run.task))
    if not b or not run.passed:
        return None
    turns, actions = len(run.turns) - b[0], len(run.actions) - b[1]
    per_turn = statistics.mean([t.seconds for t in run.turns if t.timed] or [0.0])
    return {"turns": turns, "actions": actions, "seconds": max(turns, 0) * per_turn, "best_turns": b[0]}


# -- first divergence -----------------------------------------------------------------------------------

def lcs_pairs(a, b):
    """Index pairs of a longest common subsequence of ``a`` and ``b``."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _steps(run):
    """[(turn, action)] in order, ending actions excluded."""
    return [(t, a) for t in run.turns for a in (t.actions or t.planned) if a.op not in T.END_OPS]


OP_CLASS = {"TYPE": "TYPE", "TYPE_SUBMIT": "TYPE", "SET_TEXT": "TYPE", "TAP": "TAP", "LONG_PRESS": "TAP",
            "LAUNCH_APP": "OPEN", "OPEN_URL": "OPEN", "SUBMIT": "SUBMIT", "READ_NOTIFICATIONS": "READ_NOTIFICATIONS"}


def milestone(action):
    """The action as a milestone, or None for moves that carry no decision (scrolls, dismissals, home, waits)."""
    kind = OP_CLASS.get(action.op)
    if kind is None:
        return None
    app, op, label, text = action.token()
    if kind == "OPEN":
        return ("", "OPEN", label, "")
    return (app, kind, label, text)


def milestones(passing, closest):
    """The milestones every passing run reached, in the order ``closest`` reached them."""
    sets = [{milestone(a) for _, a in _steps(r)} - {None} for r in passing]
    common = set.intersection(*sets) if sets else set()
    order = []
    for _, a in _steps(closest):
        m = milestone(a)
        if m in common and m not in order:
            order.append(m)
    return order


def divergence(fail, passing):
    """Where ``fail`` first leaves the passing path: the first milestone (an action every passing run of the task
    took) it never reached, and what it did instead at that point. ``passing``: the task's passing runs."""
    f = _steps(fail)
    f_ms = [milestone(a) for _, a in f]
    closest = max(passing, key=lambda p: (len(lcs_pairs([m for m in f_ms if m], [m for m in (milestone(a) for _, a in
                                                                                               _steps(p)) if m])),
                                          p.rubric or 0, -len(p.actions)))
    order = milestones(passing, closest)
    reached = set(f_ms)
    missed = next((m for m in order if m not in reached), None)
    p_steps = _steps(closest)
    ending = fail.turns[-1] if fail.turns else None
    turn = f_action = p_turn = p_action = None
    if missed is None:
        i0, kind = len(f), "reached_every_milestone"  # the miss is in the answer, or in an action it added
        turn = ending
    else:
        before = order[:order.index(missed)]
        hits = [i for i, m in enumerate(f_ms) if m in before]
        i0 = hits[-1] + 1 if hits else 0
        p_turn, p_action = next(((t, a) for t, a in p_steps if milestone(a) == missed), (None, None))
        # skip the failing run's moves that carry no decision: the divergence is its next real choice
        while i0 < len(f) and f_ms[i0] is None:
            i0 += 1
        if i0 >= len(f):
            turn = ending
            if ending is not None and any(a.op == "BLOCKED" for a in ending.planned):
                kind = "gave_up"
            elif ending is not None and ending.done:
                kind = "stopped_early_done"
            else:
                kind = "stopped_early_no_done"
        else:
            turn, f_action = f[i0]
            fm = f_ms[i0]
            if fm[1] == "OPEN" or missed[1] == "OPEN" or T.app_key(f_action.app) != T.app_key(p_action.app):
                kind = "different_app_or_order"
            elif fm[1] == "TYPE" and missed[1] == "TYPE" and fm[2] == missed[2]:
                kind = "different_text"
            elif fm[1] == missed[1]:
                kind = "different_element"
            else:
                kind = "different_operation"
            if p_turn is not None and turn.screen and turn.screen == p_turn.screen:
                kind += "@same_screen"
    return {"run": fail.id, "ref": closest.id, "task": fail.task, "suite": fail.suite, "category": fail.category,
            "kind": kind, "index": i0, "of": len(f), "milestones": len(order),
            "reached": sum(m in reached for m in order),
            "position": round(i0 / len(f), 3) if f else None,
            "step": turn.step if turn else None, "ref_step": p_turn.step if p_turn else None,
            "app": turn.app if turn else None, "screen": turn.title if turn else None,
            "same_screen": bool(p_turn is not None and turn is not None and turn.screen and turn.screen == p_turn.screen),
            "fail_action": [f_action.op, f_action.label[:80], f_action.app] if f_action else None,
            "ref_action": [p_action.op, p_action.label[:80], p_action.app] if p_action else None}


def divergences(runs, only_clean=True):
    """One divergence per failing judged run that has a passing run of the same task to compare with."""
    passing = collections.defaultdict(list)
    for r in runs:
        if r.passed and r.turns:
            passing[(r.suite, r.task)].append(r)
    out = []
    for r in runs:
        if not r.judged or r.passed or not r.turns or (only_clean and not r.clean):
            continue
        refs = passing.get((r.suite, r.task))
        if refs:
            out.append(divergence(r, refs))
    return out


# -- time sinks ---------------------------------------------------------------------------------------------

def time_sinks(runs, groups=None):
    """{"by_step_type": {...}, "by_app_group": {...}}: seconds by bucket, over timed turns."""
    groups = {T.app_key(k): v for k, v in (groups or {}).items()}
    by_kind, by_group = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    for r in runs:
        for t in r.turns:
            if not t.timed:
                continue
            group = groups.get(T.app_key(t.app), "other") if t.app else "none"
            for table, key in ((by_kind, t.kind), (by_group, group)):
                c = table[key]
                c["turns"] += 1
                c["model_s"] += (t.model_ms or 0.0) / 1000
                c["prep_s"] += (t.prep_ms or 0.0) / 1000
                if t.ttft_ms is not None:
                    c["ttft_turns"] += 1
                    c["ttft_s"] += t.ttft_ms / 1000
                    c["gen_s"] += (t.gen_ms or 0.0) / 1000
                if t.out_tokens:
                    c["out_tokens"] += t.out_tokens
                for a in t.actions:
                    c["actions"] += 1
                    c["exec_s"] += (a.exec_ms or 0.0) / 1000
                    c["settle_s"] += (a.settle_ms or 0.0) / 1000
                    c["refresh_s"] += (a.refresh_ms or 0.0) / 1000
    def finish(table):
        out = {}
        for key, c in table.items():
            total = c["model_s"] + c["prep_s"] + c["exec_s"] + c["settle_s"] + c["refresh_s"]
            out[key] = {"turns": c["turns"], "actions": c["actions"], "total_s": round(total, 1),
                        "s_per_turn": round(total / c["turns"], 2) if c["turns"] else None,
                        "model_s_per_turn": round(c["model_s"] / c["turns"], 2) if c["turns"] else None,
                        "ttft_s_per_turn": round(c["ttft_s"] / c["ttft_turns"], 2) if c["ttft_turns"] else None,
                        "gen_s_per_turn": round(c["gen_s"] / c["ttft_turns"], 2) if c["ttft_turns"] else None,
                        "out_tokens_per_turn": round(c["out_tokens"] / c["turns"], 1) if c["turns"] else None,
                        "exec_s_per_action": round(c["exec_s"] / c["actions"], 2) if c["actions"] else None,
                        "settle_s_per_action": round(c["settle_s"] / c["actions"], 2) if c["actions"] else None,
                        "refresh_s_per_action": round(c["refresh_s"] / c["actions"], 2) if c["actions"] else None,
                        "share": None, **{k: round(c[k], 1) for k in ("model_s", "ttft_s", "gen_s", "exec_s",
                                                                       "settle_s", "refresh_s")}}
        grand = sum(v["total_s"] for v in out.values()) or 1.0
        for v in out.values():
            v["share"] = round(v["total_s"] / grand, 3)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["total_s"]))
    return {"by_step_type": finish(by_kind), "by_app_group": finish(by_group)}


def partition(runs):
    """Where the timed seconds go, without overlap: model (first token, generation, or whole where the run has no
    stream timing), prep, and per action kind exec, settle and refresh."""
    c = collections.Counter()
    for r in runs:
        for t in r.turns:
            if not t.timed:
                continue
            if t.ttft_ms is not None:
                c["model: first token"] += t.ttft_ms / 1000
                c["model: generation"] += (t.gen_ms or 0.0) / 1000
                c["model: rest of call"] += max(0.0, (t.model_ms or 0.0) - t.ttft_ms - (t.gen_ms or 0.0)) / 1000
            else:
                c["model: whole call"] += (t.model_ms or 0.0) / 1000
            c["prep"] += (t.prep_ms or 0.0) / 1000
            for a in t.actions:
                kind = T.step_type(a.op)
                c[f"exec: {kind}"] += (a.exec_ms or 0.0) / 1000
                c["settle"] += (a.settle_ms or 0.0) / 1000
                c["refresh"] += (a.refresh_ms or 0.0) / 1000
    total = sum(c.values()) or 1.0
    return [{"bucket": k, "seconds": round(v, 1), "share": round(v / total, 3)} for k, v in c.most_common() if v > 0]


# -- time to first token -------------------------------------------------------------------------------------------

def _fit(xs, ys):
    """(intercept, slope, r) of a least-squares line, or Nones."""
    if len(xs) < 3 or len(set(xs)) < 2:
        return None, None, None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    r = statistics.correlation(xs, ys) if len(set(ys)) > 1 else None
    return my - slope * mx, slope, r


def ttft_table(runs, chars_per_token=3.0):
    """Where time to first token goes, from turns with streamed timing: cache hits, the fit on uncached tokens, and
    the turns whose output tokens exceed what their visible reply needs (hidden thinking, estimated at
    ``chars_per_token``)."""
    turns = [t for r in runs for t in r.turns if t.ttft_ms is not None and t.in_tokens]
    if not turns:
        return None
    uncached = [t.in_tokens - (t.cached_tokens or 0) - (t.write_tokens or 0) for t in turns]
    ttft = [t.ttft_ms for t in turns]
    hidden = [(t.out_tokens or 0) - (t.out_chars or 0) / chars_per_token if t.out_chars else None for t in turns]
    a, b, r = _fit(uncached, ttft)
    plain = [(u, y) for u, y, h in zip(uncached, ttft, hidden) if h is not None and h < 20]
    a2, b2, r2 = _fit([u for u, _ in plain], [y for _, y in plain])
    bands = []
    for lo, hi in ((-1e9, 20), (20, 120), (120, 250), (250, 1e9)):
        sel = [y for y, h in zip(ttft, hidden) if h is not None and lo <= h < hi]
        if sel:
            bands.append({"hidden_tokens": [None if lo < -1e8 else lo, None if hi > 1e8 else hi], "turns": len(sel),
                          "ttft_p50_ms": round(statistics.median(sel)), "ttft_mean_ms": round(statistics.mean(sel))})
    by_step = []
    for lo, hi in ((0, 1), (1, 10), (10, 20), (20, 35), (35, 10**6)):
        sel = [t.ttft_ms for t in turns if t.step is not None and lo <= t.step < hi]
        if sel:
            by_step.append({"turns_from": lo, "turns_to": hi, "n": len(sel), "ttft_p50_ms": round(statistics.median(sel))})
    p75 = statistics.quantiles(ttft, n=4)[2]
    return {"turns": len(turns), "cache_missed_turns": sum(not (t.cached_tokens or 0) for t in turns),
            "cache_write_turns": sum(bool(t.write_tokens) for t in turns),
            "cached_share_mean": round(statistics.mean((t.cached_tokens or 0) / t.in_tokens for t in turns), 3),
            "uncached_p50": statistics.median(uncached), "ttft_p50_ms": statistics.median(ttft),
            "ttft_p90_ms": statistics.quantiles(ttft, n=10)[-1], "ttft_mean_ms": round(statistics.mean(ttft)),
            "fit_ms_per_1k_uncached": round(b * 1000, 1) if b is not None else None,
            "fit_intercept_ms": round(a) if a is not None else None, "fit_r": round(r, 3) if r is not None else None,
            "fit_no_thinking_ms_per_1k": round(b2 * 1000, 1) if b2 is not None else None,
            "fit_no_thinking_r": round(r2, 3) if r2 is not None else None,
            "ms_above_p75_per_turn": round(sum(max(0.0, y - p75) for y in ttft) / len(ttft)),
            "by_hidden_thinking": bands, "by_turn_index": by_step}


# -- the ranked table -----------------------------------------------------------------------------------------

def mechanism_table(runs, use_screens=True):
    """Rows {mechanism, runs, occurrences, turns, actions, seconds, s_per_run, share_time, share_turns}, ranked."""
    rows = collections.defaultdict(lambda: {"runs": set(), "occurrences": 0, "turns": 0, "actions": 0, "seconds": 0.0})
    best = shortest(runs)
    total_s = total_turns = 0.0
    timed = [r for r in runs if r.timed]
    for r in timed:
        total_s += sum(t.seconds for t in r.turns)
        total_turns += len(r.turns)
        screens = screen_labels(r) if use_screens else None
        for mech, _, turns, actions, seconds in wasted(r, screens):
            row = rows[mech]
            row["runs"].add(r.id)
            row["occurrences"] += 1
            row["turns"] += turns
            row["actions"] += actions
            row["seconds"] += seconds
        ex = excess(r, best)
        if ex and ex["turns"] > 0:
            row = rows["excess_turns"]
            row["runs"].add(r.id)
            row["occurrences"] += 1
            row["turns"] += ex["turns"]
            row["actions"] += max(ex["actions"], 0)
            row["seconds"] += ex["seconds"]
        settle_s, n = settle_after_still(r)
        if n:
            row = rows["settle_after_still"]
            row["runs"].add(r.id)
            row["occurrences"] += n
            row["seconds"] += settle_s
        typing = [a for t in r.turns for a in t.actions if a.op in T.TYPED_OPS]
        if typing:
            row = rows["typing_exec"]
            row["runs"].add(r.id)
            row["occurrences"] += len(typing)
            row["actions"] += len(typing)
            row["seconds"] += sum((a.exec_ms or 0.0) for a in typing) / 1000
        src, reads = (r.timing or {}).get("source_seconds"), int((r.timing or {}).get("source_reads") or 0)
        if src:
            row = rows["source_reads"]
            row["runs"].add(r.id)
            row["occurrences"] += reads
            row["seconds"] += float(src)
            extra = reads - len(r.turns)  # one read per turn is the observation; the rest re-read the screen
            if extra > 0:
                row = rows["repeated_reads"]
                row["runs"].add(r.id)
                row["occurrences"] += extra
                row["seconds"] += float(src) * extra / reads
        gen = [t.gen_ms for t in r.turns if t.gen_ms is not None]
        ttft = [t.ttft_ms for t in r.turns if t.ttft_ms is not None]
        if gen:
            row = rows["generation"]
            row["runs"].add(r.id)
            row["occurrences"] += len(gen)
            row["seconds"] += sum(gen) / 1000
        if ttft:
            row = rows["first_token"]
            row["runs"].add(r.id)
            row["occurrences"] += len(ttft)
            row["seconds"] += sum(ttft) / 1000
    out = []
    for mech, row in rows.items():
        out.append({"mechanism": mech, "kind": "waste" if mech in WASTE or mech == "excess_turns" else "sink",
                    "runs": len(row["runs"]), "occurrences": row["occurrences"], "turns": row["turns"],
                    "actions": row["actions"], "seconds": round(row["seconds"], 1),
                    "s_per_run": round(row["seconds"] / len(timed), 2) if timed else None,
                    "share_time": round(row["seconds"] / total_s, 3) if total_s else None,
                    "share_turns": round(row["turns"] / total_turns, 3) if total_turns else None,
                    "feeds": HYPOTHESES.get(mech, "")})
    out.sort(key=lambda r: -r["seconds"])
    return out, {"runs": len(timed), "turns": int(total_turns), "seconds": round(total_s, 1)}


def outcome_table(runs, labels=None):
    """Failing runs: their seconds and turns by labelled cause (when labels are given) and by divergence kind."""
    by_cause = collections.defaultdict(lambda: {"runs": 0, "turns": 0, "seconds": 0.0})
    index = {pathlib.Path(r.folder).resolve(): r for r in runs}
    for lab in labels or ():
        folder = lab.get("folder")
        run = None
        for cand in (pathlib.Path(folder) if folder else None,):
            if cand is not None:
                run = index.get(cand.resolve()) if cand.is_absolute() else None
        if run is None:
            run = next((r for r in runs if r.task == lab.get("task") and r.arm == lab.get("arm")
                        and r.repeat == lab.get("run") and r.batch == lab.get("batch", r.batch)), None)
        row = by_cause[lab.get("cause")]
        row["runs"] += 1
        if run is not None:
            row["turns"] += len(run.turns)
            row["seconds"] += sum(t.seconds for t in run.turns)
    return {k: {**v, "seconds": round(v["seconds"], 1)} for k, v in sorted(by_cause.items(), key=lambda kv: -kv[1]["runs"])}


# -- report --------------------------------------------------------------------------------------------------

def _md_table(rows, cols):
    head = "| " + " | ".join(c for c, _ in cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(fmt(r) for _, fmt in cols) + " |\n" for r in rows)


def report(runs, groups=None, labels=None, title="L0 trace analytics"):
    table, totals = mechanism_table(runs)
    sinks = time_sinks(runs, groups)
    divs = divergences(runs)
    kinds = collections.Counter(d["kind"] for d in divs)
    positions = [d["position"] for d in divs if d["position"] is not None]
    judged = [r for r in runs if r.judged]
    best = shortest(runs)
    ex = [excess(r, best) for r in runs if r.passed]
    ex = [e for e in ex if e]
    lines = [f"# {title}", "",
             f"Runs: {len(runs)} ({len(judged)} judged, {sum(bool(r.passed) for r in judged)} passing); "
             f"timed: {totals['runs']} runs, {totals['turns']} turns, {totals['seconds']:.0f} s of model and device time.",
             "",
             "## Mechanisms ranked by seconds", "",
             "Each row is a ceiling on what removing that one mechanism could save; rows overlap.", ""]
    lines.append(_md_table(table, [
        ("mechanism", lambda r: r["mechanism"]), ("kind", lambda r: r["kind"]), ("runs", lambda r: str(r["runs"])),
        ("occurrences", lambda r: str(r["occurrences"])), ("turns", lambda r: str(r["turns"])),
        ("seconds", lambda r: f"{r['seconds']:.0f}"), ("s/run", lambda r: f"{r['s_per_run']:.1f}"),
        ("% time", lambda r: f"{100 * (r['share_time'] or 0):.1f}"),
        ("% turns", lambda r: f"{100 * (r['share_turns'] or 0):.1f}"), ("feeds", lambda r: r["feeds"])]))
    lines += ["", "## Where the seconds go (no overlap)", ""]
    lines.append(_md_table(partition([r for r in runs if r.timed]), [
        ("bucket", lambda r: r["bucket"]), ("seconds", lambda r: f"{r['seconds']:.0f}"),
        ("share", lambda r: f"{100 * r['share']:.1f}%")]))
    lines += ["", "## Step inefficiency (passing runs against the fewest turns of any passing run of the task)", ""]
    if ex:
        lines.append(f"Passing runs: {len(ex)}; at the shortest: {sum(e['turns'] <= 0 for e in ex)}; "
                     f"excess turns mean {statistics.mean(max(e['turns'], 0) for e in ex):.1f}, "
                     f"p50 {statistics.median(max(e['turns'], 0) for e in ex):.0f}; "
                     f"excess seconds {sum(e['seconds'] for e in ex):.0f} s in all.")
    lines += ["", "## Time sinks by step type", ""]
    cols = [("", lambda kv: kv[0]), ("turns", lambda kv: str(kv[1]["turns"])), ("total s", lambda kv: f"{kv[1]['total_s']:.0f}"),
            ("share", lambda kv: f"{100 * kv[1]['share']:.0f}%"), ("s/turn", lambda kv: str(kv[1]["s_per_turn"])),
            ("model s/turn", lambda kv: str(kv[1]["model_s_per_turn"])),
            ("ttft s", lambda kv: str(kv[1]["ttft_s_per_turn"])), ("gen s", lambda kv: str(kv[1]["gen_s_per_turn"])),
            ("out tok", lambda kv: str(kv[1]["out_tokens_per_turn"])),
            ("exec s/act", lambda kv: str(kv[1]["exec_s_per_action"])),
            ("settle s/act", lambda kv: str(kv[1]["settle_s_per_action"])),
            ("refresh s/act", lambda kv: str(kv[1]["refresh_s_per_action"]))]
    lines.append(_md_table(list(sinks["by_step_type"].items()), cols))
    lines += ["", "## Time sinks by app group", ""]
    lines.append(_md_table(list(sinks["by_app_group"].items()), cols))
    lines += ["", f"## First divergence of failing runs ({len(divs)} clean failing runs with a passing run to compare)", ""]
    if divs:
        lines.append(_md_table(kinds.most_common(), [("kind", lambda kv: kv[0]), ("runs", lambda kv: str(kv[1])),
                                                     ("share", lambda kv: f"{100 * kv[1] / len(divs):.0f}%")]))
        lines.append(f"\nWhere in the run (share of its actions done before leaving the path): "
                     f"p25 {statistics.quantiles(positions, n=4)[0]:.2f}, median {statistics.median(positions):.2f}, "
                     f"p75 {statistics.quantiles(positions, n=4)[2]:.2f}." if len(positions) >= 4 else "")
    ttft = ttft_table(runs)
    if ttft:
        lines += ["", "## Time to first token", "",
                  f"{ttft['turns']} turns with streamed timing; {ttft['cache_missed_turns']} missed the cache; "
                  f"cached share of input {100 * ttft['cached_share_mean']:.0f}%. TTFT p50 {ttft['ttft_p50_ms']:.0f} ms, "
                  f"p90 {ttft['ttft_p90_ms']:.0f} ms, mean {ttft['ttft_mean_ms']} ms. Fit on uncached tokens: "
                  f"{ttft['fit_intercept_ms']} ms + {ttft['fit_ms_per_1k_uncached']} ms per 1k (r {ttft['fit_r']}); on "
                  f"turns without hidden thinking {ttft['fit_no_thinking_ms_per_1k']} ms per 1k (r "
                  f"{ttft['fit_no_thinking_r']}).", ""]
        lines.append(_md_table(ttft["by_hidden_thinking"], [
            ("hidden thinking tokens (est.)", lambda b: f"{b['hidden_tokens'][0]}..{b['hidden_tokens'][1]}"),
            ("turns", lambda b: str(b["turns"])), ("TTFT p50 ms", lambda b: str(b["ttft_p50_ms"])),
            ("TTFT mean ms", lambda b: str(b["ttft_mean_ms"]))]))
    if labels:
        lines += ["", "## Failing runs by labelled cause", ""]
        causes = outcome_table(runs, labels)
        lines.append(_md_table(list(causes.items()), [("cause", lambda kv: str(kv[0])), ("runs", lambda kv: str(kv[1]["runs"])),
                                                      ("turns", lambda kv: str(kv[1]["turns"])),
                                                      ("seconds", lambda kv: f"{kv[1]['seconds']:.0f}")]))
    return "\n".join(lines) + "\n", {"mechanisms": table, "totals": totals, "time_sinks": sinks,
                                      "partition": partition([r for r in runs if r.timed]), "ttft": ttft,
                                      "divergences": divs, "divergence_kinds": dict(kinds),
                                      "excess": {"passing_runs": len(ex),
                                                 "at_shortest": sum(e["turns"] <= 0 for e in ex),
                                                 "excess_seconds": round(sum(e["seconds"] for e in ex), 1)}}


def select(runs, batches=None, suites=None, clean=None, models=None):
    out = []
    for r in runs:
        if batches and r.batch not in batches:
            continue
        if suites and r.suite not in suites:
            continue
        if clean is not None and r.clean != clean:
            continue
        if models and r.model not in models:
            continue
        out.append(r)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.l0", description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", nargs="+", required=True, help="batch folders, or folders holding them")
    parser.add_argument("--batches", help="comma-separated batch names to keep (default: all)")
    parser.add_argument("--suite", choices=("iosworld", "mobsterbench"), help="keep one suite")
    parser.add_argument("--app-groups", help="JSON {app name: group} for the by-app table")
    parser.add_argument("--labels", help="failure labels JSONL (the tuning loop's labeller) for the cause table")
    parser.add_argument("--title", default="L0 trace analytics")
    parser.add_argument("--out", required=True, help="folder for l0.md and l0.json")
    args = parser.parse_args(argv)
    runs = T.discover(args.runs)
    runs = select(runs, batches=set(args.batches.split(",")) if args.batches else None,
                  suites={args.suite} if args.suite else None)
    groups = json.loads(pathlib.Path(args.app_groups).read_text()) if args.app_groups else None
    labels = T.read_jsonl(args.labels) if args.labels else None
    text, data = report(runs, groups, labels, args.title)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "l0.md").write_text(text)
    (out / "l0.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

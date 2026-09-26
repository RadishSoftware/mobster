"""Offline estimates of the loop-level speed changes from existing run journals.

Reads Mobster journals (``mobster.sqlite3``, opened read-only) and prices each
change on the critical path the recorded runs actually took. Nothing touches
the phone. Numbers are what the change would have removed from those runs,
under the stated rule for each; they are not a re-run.

* swipe_guard: the pre-dispatch re-read before every swipe (decision ->
  action_started), removed outright.
* answer_at_done: answer selection and verification now start at the DONE
  decision instead of after the completion re-read. Saving per run =
  min(re-read time, time the result still waited for the answer after the
  completion check).
* predicted_decision: a transition seen before (same goal, step, screen,
  operation and target) starts the next decision at dispatch. The decision
  then overlaps dispatch acknowledgement to the next observation. Reported
  against no speculation and against the settle-time speculation
  (which hides at most ~0.25 s, latency breakdown, 23 Sep 2026).
* pixel_guard: the post-verification re-read (action_check -> action_started
  for verified actions), which a still FrameClock replaces. Upper bound: it
  applies only while the stream is healthy and the screen is still.
* open_url: web.type_url's typing route (first observation -> the settled
  page after TYPE_SUBMIT) against one WDA /url call plus a TAP-like settle
  (the /url call itself has not been timed on the phone).

Usage:
  mobile_agent/.venv/bin/python -m mobile_agent.evals.latency_offline \\
      --db path/to/state12/mobster.sqlite3 --db path/to/state13/mobster.sqlite3 \\
      [--records research/full-task-eval-2026-09-22-r4.jsonl ...]
"""

import argparse
import json
import sqlite3
import statistics

SWIPES = {"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"}
SETTLE_SPECULATION_WINDOW_MS = 250
# Assumed for a direct open: TAP dispatch p50 and TAP settle p50 (latency breakdown, 23 Sep 2026).
OPEN_URL_ASSUMED_MS = 478 + 1247


def load_runs(paths):
    runs = []
    for path in paths:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = {rid: json.loads(data) for rid, data in db.execute("select id, data from runs")}
            events = {}
            for rid, data in db.execute("select run_id, data from events order by run_id, seq"):
                events.setdefault(rid, []).append(json.loads(data))
        finally:
            db.close()
        for rid, run_events in events.items():
            info = meta.get(rid) or {}
            runs.append({"id": rid, "db": path, "goal": info.get("goal", ""), "created": info.get("createdAt", 0),
                         "events": run_events})
    runs.sort(key=lambda run: run["created"])
    return runs


def load_tasks(paths):
    tasks = {}
    for path in paths or ():
        with open(path) as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("run_id"):
                    tasks[record["run_id"]] = record.get("task")
    return tasks


def summary(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": round(statistics.median(values)), "mean": round(statistics.mean(values)),
            "p90": round(values[int(.9 * (len(values) - 1))]), "total_s": round(sum(values) / 1000, 1)}


def swipe_guard(runs):
    saved = []
    for run in runs:
        last_decision = None
        for event in run["events"]:
            if event["event"] == "decision":
                last_decision = event
            elif event["event"] == "action_started" and event.get("operation") in SWIPES and last_decision:
                if last_decision.get("operation") == event.get("operation"):
                    saved.append(event["timestamp"] - last_decision["timestamp"])
                last_decision = None
    return saved


def answer_at_done(runs):
    saved, eligible = [], 0
    for run in runs:
        events = run["events"]
        done = next((e for e in events if e["event"] == "decision" and e.get("operation") == "DONE"), None)
        if done is None:
            continue
        after = [e for e in events if e["timestamp"] >= done["timestamp"]]
        extraction = next((e for e in after if e["event"] == "inference_started" and e.get("purpose") == "extraction"),
                          None)
        check = next((e for e in after if e["event"] == "completion_check"), None)
        joined = next((e for e in after if e["event"] == "extraction_selected" and e.get("prefetched")), None)
        if not (extraction and check and joined):
            continue
        # A completion re-read that found a changed screen gets a fresh prefetch in both
        # versions. Journals from before 2026-09-23 do not say; those count as unchanged
        # (the re-ask agreed 219 of 221 times), so this is an upper bound for them.
        if check.get("same_screen") is False:
            continue
        eligible += 1
        reread = extraction["timestamp"] - done["timestamp"]
        waited = max(0.0, joined["timestamp"] - check["timestamp"])
        saved.append(min(reread, waited))
    return saved, eligible


def predicted_decision(runs):
    cache, strict_cache = {}, {}
    loose_hits = strict_hits = total = 0
    gain_vs_none, gain_vs_settle = [], []
    for run in runs:
        events = run["events"]
        step_index = 0
        for index, event in enumerate(events):
            if event["event"] != "action_started" or event.get("operation") not in SWIPES | {"TAP", "BACK"}:
                continue
            step_index += 1
            ack = next((e for e in events[index + 1:] if e["event"] == "action_acknowledged"), None)
            nxt = next((e for e in events[index + 1:] if e["event"] == "decision"), None)
            if ack is None or nxt is None or nxt.get("source") == "replay":
                continue
            started = next((e for e in events[index + 1:] if e["event"] == "inference_started"
                            and e.get("purpose") == "decision" and e["timestamp"] <= nxt["timestamp"]), None)
            total += 1
            key = (event.get("snapshot"), event["operation"], event.get("target"))
            strict = (run["goal"], step_index, *key)
            loose_hit = cache.get(key) == nxt.get("snapshot")
            strict_hit = strict_cache.get(strict) == nxt.get("snapshot")
            cache[key] = strict_cache[strict] = nxt.get("snapshot")
            loose_hits += loose_hit
            if strict_hit and started is not None:
                strict_hits += 1
                latency = nxt.get("latency_ms") or 0
                window = started["timestamp"] - ack["timestamp"]
                hidden = min(latency, max(0.0, window))
                gain_vs_none.append(hidden)
                gain_vs_settle.append(max(0.0, hidden - min(latency, SETTLE_SPECULATION_WINDOW_MS)))
    return {"transitions": total, "loose_hit_rate": round(loose_hits / total, 3) if total else None,
            "strict_hit_rate": round(strict_hits / total, 3) if total else None,
            "hidden_vs_no_speculation": summary(gain_vs_none),
            "hidden_vs_settle_speculation": summary(gain_vs_settle)}


def pixel_guard(runs):
    saved = []
    for run in runs:
        check = None
        for event in run["events"]:
            if event["event"] == "action_check":
                check = event
            elif event["event"] == "action_started" and check is not None:
                saved.append(event["timestamp"] - check["timestamp"])
                check = None
    return saved


def open_url(runs, tasks):
    costs = []
    for run in runs:
        if tasks.get(run["id"]) != "web.type_url" and "go to en.m.wikipedia.org" not in run["goal"]:
            continue
        events = run["events"]
        first = next((e for e in events if e["event"] == "observation"), None)
        typed = next((i for i, e in enumerate(events) if e["event"] == "action_started"
                      and e.get("operation") in {"TYPE_SUBMIT", "TYPE"}), None)
        if first is None or typed is None:
            continue
        settled = next((e for e in events[typed:] if e["event"] == "observation_after_action"
                        and e.get("step") == events[typed].get("step")), None)
        submit = next((i for i, e in enumerate(events) if e["event"] == "action_started"
                       and e.get("operation") == "SUBMIT"), None)
        if submit is not None:  # TYPE then SUBMIT: the route ends at the submit's settle.
            settled = next((e for e in events[submit:] if e["event"] == "observation_after_action"), settled)
        if settled is not None:
            costs.append(settled["timestamp"] - first["timestamp"])
    return costs


# Phone and model latencies for the loop simulation (p50s, latency breakdown, 23 Sep 2026 and
# vision-judge research): AX read, TAP dispatch, AX-proven settle, FrameClock settle
# (estimate), Jev decision, action verification, one VisionJudge T1 call.
SIM = {"read": .23, "tap": .48, "settle_ax": 1.0, "settle_frame": .45, "decide": .3, "verify": .2, "judge": .7}


def simulate(items=8, scale=.05, frame_clock=False):
    """Seconds per item for a swipe-deck loop: compiled LoopRunner vs the step agent.

    Both run the real code against a fake deck whose every call sleeps its
    measured p50 (times ``scale``); only the latencies are synthetic. The step
    agent cannot see photos, so its number is a floor for the same task.
    """
    import time as clock
    from unittest.mock import Mock
    from ..agent import Agent
    from ..loops import Judgment, LoopRunner, validate_program
    from ..models import Decision
    from ..state import Element, Snapshot
    from ..task_policy import ActionSupport, StopGate

    def nap(key):
        clock.sleep(SIM[key] * scale)

    class Deck:
        can_type = False

        def __init__(self, distinct=False):
            self.index, self.reads, self.taps = 0, 0, 0
            # On a real deck every card's Like button has the same accessibility path,
            # and the step agent's effect ledger refuses the second tap as a duplicate.
            self.distinct = int(distinct)

        def screen(self):
            if self.index >= items:
                return Snapshot([Element("0", "You've seen everyone", "StaticText", (.1, .4, .8, .05))],
                                "You've seen everyone", 400, 800, "wda")
            return Snapshot([Element("0", f"Person {self.index}", "StaticText", (.05, .08, .5, .05), locator="/n"),
                             Element("1", "Photo", "Image", (0, .15, 1, .55), locator="/p"),
                             Element("2", "Skip", "Button", (.05, .8, .2, .08), locator=f"/s{self.distinct * self.index}"),
                             Element("3", "Like", "Button", (.75, .8, .2, .08), locator=f"/l{self.distinct * self.index}")],
                            f"Person {self.index}", 400, 800, "wda")

        def observe(self, timeout=10):
            self.reads += 1
            nap("read")
            return self.screen()

        def execute(self, operation, target, snapshot, text=None, timeout=10):
            self.taps += 1
            nap("tap")
            self.index += 1

        def wait_for_change(self, snapshot, wait_seconds=.6, timeout=2, on_settling=None):
            clock.sleep((SIM["settle_frame"] if frame_clock else SIM["settle_ax"]) * scale)
            self.reads += 1
            return self.screen()

        def capture_preview(self, timeout=3):
            return "data:image/png;base64,UA=="

    class Judge:
        def judge(self, question, crops, choices=("yes", "no", "unsure"), context=None, timeout=4):
            nap("judge")
            return Judgment("no", .9)

    program = validate_program({
        "summary": "Like profiles", "feed": {"kind": "deck", "item": {"roles": ["StaticText"],
                                                                     "label_prefix": "Person"}},
        "predicate": {"question": "Does this person have blue eyes?", "true_choices": ["no"],
                      "false_choices": ["yes"]},
        "targets": {"like": {"label": "Like", "role": "Button"}, "skip": {"label": "Skip", "role": "Button"}},
        "branches": {"true": [{"op": "TAP", "target": "like"}], "false": [{"op": "TAP", "target": "skip"}],
                     "unsure": "skip"},
        "stop": {"count_items": items}, "policy": {"unsure_stated": True, "stop_stated": True}},
        request="like everyone without blue eyes; skip if unsure; stop after 8")
    deck = Deck()
    started = clock.monotonic()
    LoopRunner(program, driver=deck, request="like", judge=Judge(), crop=lambda image, rect: image,
               shadow=lambda screen, hint: (nap("decide"), ("TAP", "3"))[1]).run(deck.observe())
    loop_s = (clock.monotonic() - started) / scale / items
    loop_reads = deck.reads / items

    same = Deck()
    blocked = Agent(same, Mock(spec=["decide", "verify_action"], decide=Mock(side_effect=lambda *a, **k: Decision(
        "TAP", "3", .97, .05, .02, "sim", 0, {}, StopGate.CONTINUE, risk_tier="side_effect", side_effect_risk=.9)),
        verify_action=Mock(return_value=ActionSupport.ALLOWED)), settle_seconds=0).run("Like everyone", execute=True)
    deck = Deck(distinct=True)
    model = Mock(spec=["decide", "verify_action", "stable_completion"], stable_completion=True)

    def decide(snapshot, goal, history, **kwargs):
        nap("decide")
        if snapshot.text.startswith("You've"):
            return Decision("DONE", None, .99, .9, .02, "sim", 0, {}, StopGate.CONTINUE)
        return Decision("TAP", "3", .97, .05, .02, "sim", 0, {}, StopGate.CONTINUE, risk_tier="side_effect",
                        side_effect_risk=.9)

    def verify(*args, **kwargs):
        nap("verify")
        return ActionSupport.ALLOWED
    model.decide.side_effect, model.verify_action.side_effect = decide, verify
    started = clock.monotonic()
    Agent(deck, model, max_steps=items + 3, max_seconds=600).run("Like everyone", execute=True)
    step_s = (clock.monotonic() - started) / scale / items
    return {"items": items, "frame_clock": frame_clock, "loop_s_per_item": round(loop_s, 2),
            "loop_reads_per_item": round(loop_reads, 2), "step_agent_s_per_item": round(step_s, 2),
            "step_agent_reads_per_item": round(deck.reads / items, 2),
            "step_agent_on_identical_cards": f"{blocked['status']} after {same.taps} tap(s)"}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", action="append", help="A mobster.sqlite3 journal (read-only)")
    parser.add_argument("--records", action="append", help="Eval JSONL records mapping run_id to task")
    parser.add_argument("--simulate", action="store_true", help="Also time a compiled loop vs the step agent")
    args = parser.parse_args()
    if args.simulate:
        print(json.dumps([simulate(frame_clock=False), simulate(frame_clock=True)], indent=2))
    if not args.db:
        return
    runs = load_runs(args.db)
    tasks = load_tasks(args.records)
    swipes = swipe_guard(runs)
    answers, eligible = answer_at_done(runs)
    typing = open_url(runs, tasks)
    report = {
        "runs": len(runs),
        "swipe_guard_ms": summary(swipes),
        "swipe_guard_per_run_ms": round(sum(swipes) / len(runs)) if runs else None,
        "answer_at_done_ms": {**summary(answers), "eligible_runs": eligible},
        "answer_at_done_per_run_ms": round(sum(answers) / len(runs)) if runs else None,
        "predicted_decision": predicted_decision(runs),
        "pixel_guard_upper_bound_ms": summary(pixel_guard(runs)),
        "open_url": {"typing_route_ms": summary(typing), "assumed_direct_ms": OPEN_URL_ASSUMED_MS,
                     "estimated_saving_ms_p50": (round(statistics.median(typing)) - OPEN_URL_ASSUMED_MS)
                     if typing else None},
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

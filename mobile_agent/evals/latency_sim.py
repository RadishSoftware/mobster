"""Trace-driven simulation of the agent loop: the real Agent and Jev code, measured latencies.

Nothing touches the phone or the network. A fake phone (Settings root ->
General -> About) sleeps AX reads, dispatches and settles drawn from their
measured distributions; ``request_inference`` is replaced by a scripted Jev
that sleeps model latencies drawn from 1,640 recorded calls (USB iPhone
journals, 2026-09-22/23) and answers the scripted task. Every decision, guard,
speculation, hedge, memo lookup and prefetch runs through the production code,
so a change to any of them shows up here as wall-clock before it is measured
on the phone.

Scenarios switch one speed path at a time on top of the previous one:

* ``legacy``: the loop before 2026-09-24: ``baseline`` without the
  fresh-observation guard skip and without answers started from speculative
  DONE decisions.
* ``baseline``: hedging, first-screen prediction and the decision memo off;
  the first decision of a run pays the cold-connection distribution.
* ``warm``: + connections warmed before the first decision (first call drawn
  from the warm distribution).
* ``hedge``: + hedged critical-path calls (hedge.py).
* ``predict``: + first-screen prediction (the app's remembered first screen).
* ``memo``: + the decision memo, measured on a repeat of an identical request.
* ``frameclock``: a PROJECTION, not a measurement: ``memo`` plus the FrameClock
  "on" settle as simulated in the FrameClock validation of 23 Sep 2026 (tap settle
  ~0.65 s p50, a first observation of one read when the screen is still).

Usage:
  mobile_agent/.venv/bin/python -m mobile_agent.evals.latency_sim [--runs 30] [--scale .5] [--task nav|answer]
"""

import argparse
import json
import os
import random
import statistics
import threading
import time
from unittest.mock import patch

# Latency quantiles (ms) at 0, 5, 10, 20, ..., 90, 95, 98, 99, 100 %.
KNOTS = (0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .98, .99, 1)
MODEL = {
    "decision_first": (199, 255, 268, 300, 318, 350, 416, 540, 809, 1102, 1524, 2039, 2943, 3926, 5937),
    "decision": (122, 155, 164, 183, 200, 219, 251, 294, 394, 592, 759, 1020, 1503, 1818, 3013),
    "extraction": (141, 199, 217, 246, 287, 351, 465, 605, 845, 983, 1261, 1590, 2120, 2143, 5837),
    "verification": (118, 142, 159, 213, 246, 264, 298, 360, 503, 705, 921, 1072, 1490, 1696, 2320),
    "action_verification": (88, 128, 135, 169, 184, 208, 297, 374, 458, 599, 777, 813, 970, 1230, 1731),
}
# Phone (latency breakdown, 23 Sep 2026 p50/p90, spread to the same shape).
PHONE = {
    "read": (130, 150, 160, 180, 195, 210, 234, 260, 300, 380, 502, 560, 650, 700, 800),
    "tap": (440, 450, 455, 462, 468, 473, 478, 484, 492, 503, 517, 525, 540, 550, 600),
    "tap_settle": (900, 960, 990, 1030, 1100, 1180, 1247, 1300, 1360, 1410, 1459, 1480, 1495, 1500, 1520),
}
QUIET = .5  # The AX quiet period of observe_ready and wait_for_change.
# FrameClock "on" (projection): push animation + 0.18 s still + glass lag + one read.
FRAME_SETTLE = (520, 560, 580, 600, 620, 635, 650, 670, 690, 720, 760, 800, 900, 1000, 1500)


def draw(table, rng):
    u = rng.random()
    for index in range(1, len(KNOTS)):
        if u <= KNOTS[index]:
            lo, hi = KNOTS[index - 1], KNOTS[index]
            return table[index - 1] + (table[index] - table[index - 1]) * (u - lo) / (hi - lo)
    return table[-1]


class Clock:
    def __init__(self, scale, seed):
        self.scale, self.rng, self.lock = scale, random.Random(seed), threading.Lock()

    def ms(self, table):
        with self.lock:
            return draw(table, self.rng)

    def sleep(self, ms):
        time.sleep(ms * self.scale / 1000)


def screens():
    from ..state import Element, Snapshot

    def snap(title, rows):
        elements = [Element("0", title, "StaticText", (.3, .06, .4, .04), locator=f"/{title}/title")]
        for index, (label, value) in enumerate(rows, 1):
            elements.append(Element(str(index), label, "Cell" if not value else "StaticText",
                                    (.05, .15 + .07 * index, .9, .06), locator=f"/{title}/{index}", value=value))
        text = "\n".join([title] + [f"{label} {value}".strip() for label, value in rows])
        return Snapshot(elements, text, 393, 852, "wda", bundle_id="com.apple.Preferences")
    return {
        "root": snap("Settings", [("General", ""), ("Accessibility", ""), ("Privacy & Security", "")]),
        "general": snap("General", [("About", ""), ("Software Update", ""), ("Keyboard", "")]),
        "about": snap("About", [("Name", "iPhone"), ("iOS Version", "26.0.1"), ("Model Name", "iPhone 15 Pro")]),
    }


class Phone:
    """A fake WDA-like driver: reads, dispatches and settles take measured time."""
    can_type = False
    reports_settling = True
    host_operations = frozenset()

    def __init__(self, clock, frame_clock=False):
        self.clock, self.screens, self.at = clock, screens(), "root"
        self.frame = frame_clock
        self.links = {("root", "General"): "general", ("general", "About"): "about"}
        self.last_image = None
        self.stable_at = None

    def read(self):
        from dataclasses import replace
        self.clock.sleep(self.clock.ms(PHONE["read"]))
        return replace(self.screens[self.at], captured_at=time.monotonic())

    def observe(self, timeout=10):
        return self.read()

    def observe_ready(self, timeout=10):
        # Like WDA.observe_ready: the read that just proved rest is reused
        # (WDA_STABLE_REUSE_SECONDS); otherwise a read plus the quiet proof.
        if self.stable_at is not None and time.monotonic() - self.stable_at[0] <= .5 * self.clock.scale:
            return self.stable_at[1]
        first = self.read()
        if not self.frame:
            self.clock.sleep(QUIET * 1000)
        self.stable_at = (time.monotonic(), first)
        return first

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.stable_at = None
        self.clock.sleep(self.clock.ms(PHONE["tap"]))
        self.at = self.links.get((self.at, target.label), self.at)
        return {}

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None):
        total = self.clock.ms(FRAME_SETTLE if self.frame else PHONE["tap_settle"])
        # Two agreeing reads arrive about one quiet period before the proof,
        # which ends with one more read.
        read = self.clock.ms(PHONE["read"])
        quiet = 0 if self.frame else QUIET * 1000
        self.clock.sleep(max(0.0, total - quiet - read))
        if on_settling is not None:
            on_settling(self.screens[self.at])
        self.clock.sleep(min(total, quiet))
        settled = self.read()
        self.stable_at = (time.monotonic(), settled)
        return settled

    def close(self):
        pass


def scripted_inference(clock, cold_first):
    """A request_inference stand-in: scripted answers, measured latencies."""
    state = {"first": cold_first}
    lock = threading.Lock()

    def choice(options, pick):
        probabilities = {key: (.97 if key == pick else .03 / max(1, len(options) - 1)) for key in options}
        return {"type": "choice", "choice": pick, "confidence": .97, "probabilities": probabilities}

    def answer_decision(body):
        questions, screen = body["questions"], body["state"]
        title = next((e.get("label") for e in screen["elements"] if e.get("role") == "StaticText"), "")
        want = {"Settings": "General", "General": "About"}.get(title)
        op = "TAP" if want else "DONE"
        answers = {"operation": choice(questions["operation"]["criteria"], op),
                   "goal": {"type": "noul", "noul": .95 if op == "DONE" else .05},
                   "blocked": {"type": "noul", "noul": .02},
                   "stop_gate": choice({"continue": 0, "stop": 0, "unclear": 0}, "continue"),
                   "screen_has_loading": {"type": "noul", "noul": .01},
                   "side_effect_risk": {"type": "score", "score": .02}}
        if "tap_target" in questions:
            criteria = questions["tap_target"]["criteria"]
            pick = next((key for key, text in criteria.items() if want and f'"label": "{want}"' in text), "none")
            answers["tap_target"] = choice(criteria, pick)
        if "output_intent" in questions:
            answers["output_intent"] = choice(questions["output_intent"]["criteria"], "action_only")
        return answers

    def answer_extraction(body):
        answers = {}
        for name, question in body["questions"].items():
            criteria = question["criteria"]
            pick = next((key for key, text in criteria.items() if '"literal": "26.0.1"' in text), "none")
            answers[name] = choice(criteria, pick)
        return answers

    def fake(http, path, body, timeout, *, emit, provider, call_id, model, purpose):
        with lock:
            table = "decision_first" if purpose == "decision" and state["first"] else purpose
            if purpose == "decision":
                state["first"] = False
        started = time.monotonic()
        clock.sleep(clock.ms(MODEL.get(table, MODEL["decision"])))
        if emit is not None:
            emit({"event": "inference_finished", "purpose": purpose, "call_id": call_id,
                  "latency_ms": (time.monotonic() - started) * 1000, "success": True, "provider": provider})
        if purpose == "decision":
            answers = answer_decision(body)
        elif purpose == "extraction":
            answers = answer_extraction(body)
        elif purpose == "verification":
            answers = {"output_support": choice({"supported": 0, "unsupported": 0, "unclear": 0}, "supported"),
                       "claim_final_state": {"type": "noul", "noul": .95},
                       "claim_entity": {"type": "noul", "noul": .95},
                       "claim_field": {"type": "noul", "noul": .95}}
        else:
            answers = {"action_support": choice({"allowed": 0, "mismatch": 0, "unclear": 0}, "allowed")}
        return {"answers": answers, "model": "jev-sim", "usage": {}}
    return fake


def run_once(scenario, clock, task, memo_store):
    from .. import agent as agent_module, hedge, models
    from ..agent import Agent
    from ..latency_trace import Trace

    cold = scenario in {"baseline", "legacy"}
    legacy = scenario == "legacy"
    saved = (agent_module.FRESH_GUARD_SECONDS, Agent._speculate_answer)
    if legacy:
        agent_module.FRESH_GUARD_SECONDS = -1.0
        Agent._speculate_answer = lambda self, *args, **kwargs: None
    hedging = scenario in {"hedge", "predict", "memo", "frameclock"}
    predict = scenario in {"predict", "memo", "frameclock"}
    models.Jev.hedging = hedging
    os.environ.setdefault("TYPESAFE_API_KEY", "sim")
    phone = Phone(clock, frame_clock=scenario == "frameclock")
    trace = Trace()
    events = []
    schema = ({"type": "object", "properties": {"ios_version": {"type": "string"}}, "required": ["ios_version"],
               "additionalProperties": False} if task == "answer" else None)
    goal = ("In Settings: report the iOS version shown in General > About" if task == "answer"
            else "In Settings: open General > About")
    with patch.object(models, "request_inference", scripted_inference(clock, cold)):
        model = models.Jev(key="sim")
        agent = Agent(phone, model, emit=events.append, trace=trace, max_seconds=120,
                      decision_memo=memo_store if scenario in {"memo", "frameclock"} else None,
                      launch_bundle="com.apple.Preferences" if predict else None)
        started = time.monotonic()
        try:
            result = agent.run(goal, execute=True, output_schema=schema)
        finally:
            agent_module.FRESH_GUARD_SECONDS, Agent._speculate_answer = saved
        wall = (time.monotonic() - started) * 1000 / clock.scale
    return wall, result["status"], trace.event()


def simulate(runs=30, scale=.5, task="nav", seed=7,
             scenarios=("legacy", "baseline", "warm", "hedge", "predict", "memo", "frameclock")):
    from .. import agent as agent_module, hedge
    from ..decision_memo import DecisionMemo

    out = {}
    for scenario in scenarios:
        clock = Clock(scale, seed)
        # Hedge thresholds are wall-clock seconds: scale them with the simulation.
        hedge.HEDGERS["typesafe"] = hedge.Hedger(floor={k: v * scale for k, v in hedge.JEV_FLOOR.items()},
                                                 default={k: v * scale for k, v in hedge.JEV_DEFAULT.items()})
        with agent_module._first_screens_lock:
            agent_module._first_screens.clear()
        memo_store = DecisionMemo()
        walls, statuses, hidden = [], {}, []
        warmup = 1 if scenario in {"predict", "memo", "frameclock"} else 0
        for index in range(runs + warmup):
            if scenario in {"memo", "frameclock"}:
                memo_store = memo_store if index else DecisionMemo()
            wall, status, trace = run_once(scenario, clock, task, memo_store)
            if index < warmup:
                continue
            walls.append(wall)
            statuses[status] = statuses.get(status, 0) + 1
            hidden.append(trace["summary"]["overlap_ms"] / scale)
        walls.sort()
        q = lambda p: walls[min(len(walls) - 1, int(p * (len(walls) - 1)))]
        out[scenario] = {"runs": len(walls), "mean_ms": round(statistics.mean(walls)), "p50_ms": round(q(.5)),
                         "p90_ms": round(q(.9)), "p99_ms": round(q(.99)), "statuses": statuses,
                         "hedges": hedge.HEDGERS["typesafe"].budget.hedges,
                         "overlap_ms_mean": round(statistics.mean(hidden))}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--scale", type=float, default=.5, help="Sleep scale (1 = real time)")
    parser.add_argument("--task", choices=("nav", "answer"), default="nav")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--scenario", action="append", help="Only these scenarios")
    args = parser.parse_args()
    scenarios = tuple(args.scenario) if args.scenario else ("legacy", "baseline", "warm", "hedge", "predict",
                                                            "memo", "frameclock")
    print(json.dumps(simulate(args.runs, args.scale, args.task, args.seed, scenarios), indent=2))


if __name__ == "__main__":
    main()

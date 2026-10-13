"""Benchmark the replay core on the scripted world: a fresh Smart run against a replay of its recorded path, and a
replay that heals one changed step. No phone, no simulator, no key, no network.

    mobile_agent/.venv/bin/python scripts/bench_replay.py            # 10 runs of each, the table on stdout
    mobile_agent/.venv/bin/python scripts/bench_replay.py --runs 3 --json

What is real and what is modeled:

- Real: Mobster's own code end to end. Checks go through VerifyRun (prepare, the flow, the verdict read on the tree,
  the report) with the real FrontierAgent; workflows through FrontierAgent and the replay engine.
- Modeled, the phone: each read and action sleeps the times measured on a USB iPhone (the long-form scripted world's
  model, DESIGN.md 12.3: a tree read 0.35 s, a tap 0.25 s, typing 0.6 s; verify's evidence read 0.76 s, measured 28 Sep
  2026 on an iPhone 17 Pro simulator, verify/tree.py).
- Modeled, the model: each Smart call sleeps 2.5 s (a Sonnet turn) and the task contract 1.5 s (the same model). Its
  cost is the real prompt the frontier built, at 4 characters a token, plus 150 output tokens a call, at
  claude-sonnet-5-5's list price with no prompt caching (frontier.PRICES). No screenshots are sent in this world, so a
  real run's image tokens are missing: the fresh cost is a floor, never an overstatement.
"""

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL = "claude-sonnet-5-5"
TURN_SECONDS, CONTRACT_SECONDS = 2.5, 1.5
OUTPUT_TOKENS = 150
PHONE = {"observe": .35, "TAP": .25, "TYPE": .6, "evidence": .76}


def priced(model):
    """(calls, cost in USD) of a scripted model's calls, at MODEL's list price."""
    from mobile_agent.frontier import cost_usd
    calls = model.calls + model.contract_calls
    usage = {"prompt_tokens": model.prompt_chars // 4, "completion_tokens": OUTPUT_TOKENS * calls}
    return calls, (cost_usd(MODEL, usage) or 0.0) if calls else 0.0


class Thinking:
    """A scripted model that takes a real model's time per call."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, messages, schema, **kwargs):
        contract = "items" in schema.get("properties", {})
        time.sleep(CONTRACT_SECONDS if contract else TURN_SECONDS)
        return self.inner.complete(messages, schema, **kwargs)


# -- checks: VerifyRun on the Lumen sign-up -------------------------------------------------------------------

def check_runs(runs):
    from mobile_agent import engines
    from mobile_agent.tests import replay_world as W
    from mobile_agent.verify import runner
    from mobile_agent.verify.checks import dump_check, load_check
    W.LATENCY.update(PHONE)
    W.PACE["value"] = 1.0
    folder = Path(tempfile.mkdtemp(prefix="mobster-bench-replay-"))
    checks = folder / ".mobster" / "checks"
    checks.mkdir(parents=True)
    file = checks / "signup.yaml"
    file.write_text(dump_check(W.check()), encoding="utf-8")
    cache = folder / ".mobster" / "cache" / "replay"
    state = W.new_state()
    phone, manager = W.LumenPhone(state), W.Manager(state)
    models = []

    def client(key=None, **_):
        model = W.LumenModel()
        models.append(model)
        return Thinking(model)

    def one(replay):
        before = len(models)
        run = runner.VerifyRun(load_check(file), mode="smart", runs_dir=folder / ".mobster" / "runs",
                               manager=manager, key="sk-bench-never-sent", replay=replay)
        started = time.monotonic()
        with mock.patch.object(runner, "build_driver", lambda *args: phone), \
                mock.patch.object(engines, "build_client", client), \
                mock.patch.object(engines, "smart_key", lambda env=None: "sk-bench-never-sent"), \
                mock.patch.object(runner, "front_app", lambda url, timeout=5: (W.BUNDLE, manager.pid)):
            run.prepare()
            run.run_flow()
            result = run.finish()
        seconds = time.monotonic() - started
        calls, cost = 0, 0.0
        for model in models[before:]:
            c, usd = priced(model)
            calls, cost = calls + c, cost + usd
        return {"verdict": result["verdict"], "seconds": seconds, "flow": result["timing"].get("flow"),
                "calls": calls, "costUsd": cost, "mode": (result.get("replay") or {}).get("mode"),
                "healed": len(result.get("healed") or ())}

    out = {"fresh": [], "replay": [], "heal": []}
    for _ in range(runs):
        state["build"] = 1
        out["fresh"].append(one("fresh"))
    recorded = {path.name: path.read_bytes() for path in cache.glob("*.json")}
    for _ in range(runs):
        out["replay"].append(one("auto"))
    for _ in range(runs):
        for name, data in recorded.items():  # each heal run starts from the build-1 recording
            (cache / name).write_bytes(data)
        for path in checks.glob("*.heal.yaml"):
            path.unlink()
        state["build"] = 2
        out["heal"].append(one("auto"))
    state["build"] = 1
    return out


# -- workflows: FrontierAgent against the replay engine, on sota_world's phone ------------------------------------

WORKFLOWS = (("Notes: save a note", "com.apple.mobilenotes", "Save a new note: Luca, 214 Pine St (dinner Friday 7:30)"),
             ("Messages: text Alex (asks first)", "com.apple.MobileSMS", "Text Alex: Running 10 minutes late, sorry!"))


def workflow_runs(runs):
    import re
    from mobile_agent import engines
    from mobile_agent.replay_core.engine import Replayer
    from mobile_agent.replay_core.path import new_path
    from mobile_agent.replay_core.recorder import Recorder, literal_text
    from mobile_agent.tests import sota_world as world
    original = world.decide

    def decide(t):  # sota_world's model, plus "Save a new note: <text>"
        if re.match(r"save a new note: (.+)$", t.r):
            body = t.request.split(": ", 2)[-1].strip()
            if not t.in_app(world.NOTES):
                return [world.launch(world.NOTES)], None, []
            if t.has("New Note"):
                if any(label.startswith(body[:60]) for label in t.labels()):
                    return world.done("Saved.")
                return [world.act("TAP", t.find("New Note"))], None, []
            note = t.find("Note", "TextView")
            if note and body not in t.prompt.split("Screen elements:", 1)[1]:
                return [world.act("TYPE", note, body)], None, []
            return [world.act("TAP", t.find("Done"))], None, []
        return original(t)

    class Phone(world.ScriptedPhone):
        def observe(self, timeout=10):
            time.sleep(PHONE["observe"])
            return super().observe(timeout)

        def execute(self, operation, target, snapshot, text=None, timeout=10):
            time.sleep(PHONE["TYPE"] if operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") else PHONE["TAP"])
            return super().execute(operation, target, snapshot, text=text, timeout=timeout)

    world.PACE["value"] = 0.0
    out = {}
    with mock.patch.object(world, "decide", decide):
        for name, bundle, goal in WORKFLOWS:
            fresh, replay, path = [], [], None
            for _ in range(runs):
                state = world.new_phone_state()
                phone = Phone(state)
                phone.execute("LAUNCH_APP", bundle, None)
                model = world.ScriptedModel()
                counted = {"calls": 0, "chars": 0}

                class Counting(Thinking):
                    def complete(self, messages, schema, **kwargs):
                        from mobile_agent.frontier import prompt_text
                        counted["calls"] += 1
                        counted["chars"] += len(prompt_text(messages))
                        return super().complete(messages, schema, **kwargs)
                recorder = Recorder(text_policy=literal_text([goal]))
                agent = engines.build_frontier(recorder.wrap(phone), Counting(model), apps=world.APP_NAMES,
                                               emit=lambda event: None, approve=lambda request: "approved",
                                               step_observer=recorder.observe_step)
                started = time.monotonic()
                outcome = agent.run(f"In {world.APP_NAMES[bundle]}: {goal}")
                seconds = time.monotonic() - started
                from mobile_agent.frontier import cost_usd
                cost = cost_usd(MODEL, {"prompt_tokens": counted["chars"] // 4,
                                        "completion_tokens": OUTPUT_TOKENS * counted["calls"]}) or 0.0
                fresh.append({"status": outcome.get("status"), "seconds": seconds, "calls": counted["calls"],
                              "costUsd": cost})
                path = new_path(recorder.steps, kind="workflow", identity={"bench": name}, env={})
            for _ in range(runs):
                state = world.new_phone_state()
                phone = Phone(state)
                phone.execute("LAUNCH_APP", bundle, None)
                started = time.monotonic()
                result = Replayer(phone, approve=lambda request: "approved").run(path)
                replay.append({"status": result.status, "seconds": time.monotonic() - started, "calls": 0,
                               "costUsd": 0.0, "replayed": result.replayed})
            out[name] = {"fresh": fresh, "replay": replay}
    return out


def median(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.median(values) if values else None


def table(checks, workflows, runs):
    lines = [f"Replay core on the scripted world, n = {runs} each, {time.strftime('%Y-%m-%d')}, "
             f"model time and cost modeled ({MODEL}, list price, no cache)", "",
             "| Flow | Mode | Median time | Model calls | Model cost | Result |", "|---|---|---|---|---|---|"]

    def row(flow, mode, rows, result_key):
        results = sorted({str(r.get(result_key)) for r in rows})
        lines.append(f"| {flow} | {mode} | {median(rows, 'seconds'):.1f} s | {median(rows, 'calls'):g} | "
                     f"${median(rows, 'costUsd'):.4f} | {', '.join(results)} |")
    row("Check: Lumen sign-up (VerifyRun)", "fresh (Smart)", checks["fresh"], "verdict")
    row("Check: Lumen sign-up (VerifyRun)", "replay", checks["replay"], "verdict")
    row("Check: Lumen sign-up, button renamed", "replay + 1 heal", checks["heal"], "verdict")
    for name, data in workflows.items():
        row(f"Workflow: {name}", "fresh (Smart)", data["fresh"], "status")
        row(f"Workflow: {name}", "replay", data["replay"], "status")
    fresh, replay = median(checks["fresh"], "seconds"), median(checks["replay"], "seconds")
    lines += ["", f"Check: replay is {fresh / replay:.1f}x faster than fresh "
                  f"({fresh:.1f} s → {replay:.1f} s, the verdict read and report included), and costs $0."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark the replay core on the scripted world.")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = check_runs(args.runs)
    workflows = workflow_runs(args.runs)
    if args.json:
        print(json.dumps({"checks": checks, "workflows": workflows}, indent=1))
    else:
        print(table(checks, workflows, args.runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

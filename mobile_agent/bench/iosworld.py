"""iOSWorld, run by Mobster: the published iOS agent benchmark, on its own terms.

iOSWorld (arXiv 2606.09764; github.com/ljang0/iOSWorld, CC BY 4.0): 133 tasks over 26
SwiftUI apps on an iPhone 17 Pro simulator, scored by its own LLM judge (gpt-5.4-mini by
default) from each run's trajectory -- per-step screenshots, actions, final answer --
against the task's rubric. Published best: 51.9% (Claude Opus 4.6 + XCUITest tree).

What is iOSWorld's and unchanged: the tasks (goals verbatim), the start state (its own
``reset_app_data`` + ``reseed_apps``, then Home, as its runner does), the 50-step limit,
the trajectory format and the judge (``scripts/judge_trajectories.py``, run as is).

What is Mobster's: the agent, composed as the bench's ``mobster-next`` (Jev, the helper,
FrameClock, VisionJudge), driving the simulator through WDA. The task's app list becomes
Mobster's allowed apps -- iOSWorld hands its own agent the same list -- and the first one
is opened, recorded as a ``launch_app`` step as iOSWorld's agent would take it.
``--policy frontier`` runs mobile_agent.frontier.FrontierAgent (a frontier model per step
over the same driver) instead, capped at MAX_TASK_COST_USD per task.

Screenshots exist only for the judge: taken with ``simctl io`` (never through WDA, never
shown to Mobster) before each action and once at the end; their time is excluded from
Mobster's reported latency. The simulator runs headless: nothing here opens a window.

    python -m mobile_agent.bench.iosworld run --repo ../iOSWorld --udid <UDID> \\
        --wda-url http://127.0.0.1:8200 --mjpeg-url http://127.0.0.1:9200 \\
        --out research/iosworld-runs/NAME [--only caltrack-001,...] [--limit N] --env-file <agent.env> \\
        [--policy frontier --model gpt-5.6-luna --reasoning low]
    python -m mobile_agent.bench.iosworld pool --repo ../iOSWorld --sims UDID:WDA_PORT:MJPEG_PORT,... \\
        --out research/iosworld-runs/NAME [--only ...] [--limit N] [--policy frontier ...] --env-file <agent.env>
    python -m mobile_agent.bench.iosworld judge --repo ../iOSWorld --run research/iosworld-runs/NAME
    python -m mobile_agent.bench.iosworld report --run research/iosworld-runs/NAME
"""

import argparse
import base64
import dataclasses
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
import urllib.request

MAX_STEPS = 50  # iOSWorld's runner default (--max-steps 50, no task timeout)
MAX_SECONDS = 900
DEFAULT_FRONTIER_MODEL = "gpt-5.6-luna"
MAX_TASK_COST_USD = 0.60  # a runaway task stops here (24 Sep: 50-step loops cost ~$0.80 each on gpt-5.5)


def task_cost_cap(bundles):
    """A spend cap that grows with the apps a task spans: a flat $0.25 cut five-app tasks off
    mid-way (multi-006, mem-005, 24 Sep) while one-app tasks finish for $0.03-0.13."""
    return min(MAX_TASK_COST_USD, 0.10 + 0.08 * max(1, len(bundles)))
TASK_DIR_WIDTH = 3
_APP_ALIASES = {"letterboxd": "cinephile", "whatsapp": "quickchat", "linkedin": "lockedin"}


# -- iOSWorld's side ---------------------------------------------------------------------

def load_tasks(repo):
    return json.loads((pathlib.Path(repo) / "tasks.json").read_text(encoding="utf-8"))


def load_manifest(repo):
    """{app: {bundle_id, app_path, ...}} written by iOSWorld's bootstrap."""
    for candidate in sorted(pathlib.Path(repo).rglob(".app_manifest.json")):
        return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError("No .app_manifest.json: run iOSWorld's bootstrap first")


def register_names(manifest):
    """Each app's display name (its Info.plist), so Jev's app-switch choices read "CalTrack", not a bundle."""
    import plistlib
    from ..catalog import register_app_names
    names = {}
    for key, entry in manifest.items():
        name = key
        try:
            with open(pathlib.Path(entry.get("app_path", "")) / "Info.plist", "rb") as handle:
                info = plistlib.load(handle)
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or key
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        if entry.get("bundle_id"):
            names[entry["bundle_id"]] = name
    register_app_names(names)
    return names


def iosworld_module(repo):
    """iOSWorld's own runner module (stdlib-only at import), for its reset functions."""
    scripts = str(pathlib.Path(repo) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import appium_agent
    return appium_agent


def app_entry(manifest, name):
    return manifest.get(_APP_ALIASES.get(name, name)) or manifest.get(name) or {}


RESET_ATTEMPTS = 2


def reset(repo, udid, manifest):
    """iOSWorld's clean slate: wipe every app's data, re-seed each, as its runner does per task."""
    runner = iosworld_module(repo)
    apps = list(manifest)
    for attempt in range(RESET_ATTEMPTS):
        try:
            runner.reset_app_data(udid, apps, manifest)
            runner.reseed_apps(udid, apps, manifest)
            return
        except RuntimeError:
            # Seeding under load can leave apps empty ("no seed data after 3 attempts": four
            # simulators busy, 24 Sep); a full second pass cleared it.
            if attempt + 1 == RESET_ATTEMPTS:
                raise


# -- screenshots for the judge ---------------------------------------------------------

class Shots:
    """PNG screenshots through ``simctl io`` (not WDA, so Mobster's device channel is untouched)."""

    def __init__(self, udid, folder):
        self.udid, self.folder = udid, pathlib.Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.seconds = 0.0
        self.trace = None  # the driver's call_trace: shots go in it, to be told apart from the agent's time

    def take(self, name):
        started = time.monotonic()
        path = self.folder / f"{name}.png"
        try:
            subprocess.run(["xcrun", "simctl", "io", self.udid, "screenshot", "--type=png", str(path)],
                           check=True, capture_output=True, timeout=20)
        except (subprocess.SubprocessError, OSError):
            path = None
        self.seconds += time.monotonic() - started
        self.count += 1
        if self.trace is not None:
            self.trace.append(("shot", started, (time.monotonic() - started) * 1000))
        return str(path) if path else None


# -- the trajectory ----------------------------------------------------------------------

def describe(operation, target=None, text=None):
    """One iOSWorld-style action dict for a Mobster operation."""
    if operation == "LAUNCH_APP":
        return {"type": "launch_app", "bundle_id": str(target)}
    if operation.startswith("SWIPE_"):
        return {"type": "swipe", "direction": operation.split("_", 1)[1].lower()}
    if operation in ("HOME", "BACK", "VOLUME_UP", "VOLUME_DOWN"):
        return {"type": operation.lower()}
    action = {"type": {"TAP": "tap", "TYPE": "type", "TYPE_SUBMIT": "type", "SUBMIT": "submit",
                       "LONG_PRESS": "long_press"}.get(operation, operation.lower())}
    if target is not None and hasattr(target, "label"):
        action["element"] = {"label": (target.label or "")[:120], "role": target.role}
    if text is not None:
        action["text"] = text
    if operation == "TYPE_SUBMIT":
        action["submit"] = True
    return action


class RecordingDriver:
    """The simulator's WDA driver, with each dispatch recorded as an iOSWorld trajectory step."""

    def __init__(self, driver, shots):
        self._driver, self.shots = driver, shots
        self.steps = []
        # The model turn now acting (set from frontier_decision events): screenshots are named by
        # it, since a turn may dispatch several actions and a refused turn none.
        self.turn = None

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def _record(self, action):
        """Screenshot and record one step; returns the screenshot's seconds."""
        index = len(self.steps) + 1
        name = f"step-{index:03d}" + (f"-turn-{self.turn:02d}" if self.turn is not None else "")
        started = time.monotonic()
        self.steps.append({"step": index, "screenshot": self.shots.take(name), "actions": [action],
                           **({"turn": self.turn} if self.turn is not None else {})})
        return time.monotonic() - started

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        spent = self._record(describe(operation, target, text))
        # The judge's screenshot (0.2-2.5 s) does not age the observation: without it the action goes
        # that much sooner, so the driver's re-read of an observation over 1 s old (WDA_FRESH_SECONDS)
        # would not run (it ran after 0.6-1.8 s screenshots in exec-new2's megamart-001).
        if hasattr(snapshot, "captured_at"):
            snapshot = dataclasses.replace(snapshot, captured_at=snapshot.captured_at + spent)
        return self._driver.execute(operation, target, snapshot, text=text, timeout=timeout)

    def long_press(self, target, snapshot, timeout=10):
        self._record({"type": "long_press", "element": {"label": (target.label or "")[:120], "role": target.role}})
        return self._driver.long_press(target, snapshot, timeout=timeout)

    def tap_point(self, x, y, snapshot, timeout=10):
        self._record({"type": "tap_xy", "x": round(x, 3), "y": round(y, 3), "purpose": "dismiss"})
        return self._driver.tap_point(x, y, snapshot, timeout=timeout)

    def set_value(self, target, value, timeout=10):
        self._record({"type": "set_picker", "element": {"label": (target.value or "")[:120], "role": target.role},
                      "value": str(value)[:60]})
        return self._driver.set_value(target, value, timeout=timeout)

    def clear_text(self, target, timeout=10):
        self._record({"type": "clear_text", "element": {"label": (target.label or "")[:120], "role": target.role}})
        return self._driver.clear_text(target, timeout=timeout)

    def replace_text(self, target, text, timeout=10):
        self._record({**describe("TYPE", target, text), "replace": True})
        done = self._driver.replace_text(target, text, timeout=timeout)
        if not done:
            self.steps.pop()  # nothing was typed: the caller clears and types, recorded as such
        return done

    def call(self, method, path, body=None, timeout=10):
        if method == "POST" and path == "/wda/apps/activate" and isinstance(body, dict):
            self._record(describe("LAUNCH_APP", body.get("bundleId")))
        elif method == "POST" and path == "/url" and isinstance(body, dict):
            self._record({"type": "open_url", "url": str(body.get("url", ""))[:300]})
        return self._driver.call(method, path, body, timeout)


def judge_shots_apart(timing, seconds):
    """The frontier's timing with the judge's screenshots (taken before each dispatch, inside its
    exec bucket) moved to their own bucket: they are not the agent's time (0.4-2.5 s each)."""
    if not timing or "exec" not in timing:
        return timing
    return {**timing, "exec": round(timing["exec"] - seconds, 2), "judge_shots": round(seconds, 2)}


def answer_text(result):
    data = result.get("data")
    if data is None:
        return None
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False)


# -- one task ------------------------------------------------------------------------------

def clear_alerts(driver, attempts=5):
    """Accept system prompts the re-seed launches left up (CityRide's location prompt covered
    CalTrack in the first smoke run). iOSWorld's bootstrap clicks these with AppleScript, which
    needs the Simulator window; here WDA's alert endpoint does it headlessly, before the task."""
    from ..transport import TransportError
    for _ in range(attempts):
        try:
            text = driver.call("GET", "/alert/text", timeout=5)
        except TransportError:
            return
        if not text:
            return
        try:
            driver.call("POST", "/alert/accept", {"name": "Allow While Using App"}, timeout=5)
        except TransportError:
            driver.call("POST", "/alert/accept", {}, timeout=5)
        time.sleep(.5)


def wda_session(wda_url):
    # Not drivers.resolve_wda_session: that one creates sessions with shouldWaitForQuiescence=False and
    # waitForIdleTimeout=0, which changes how WDA settles on the simulator; swap only after a measured
    # iOSWorld smoke run. (This one also reads only a top-level sessionId.)
    status = json.load(urllib.request.urlopen(wda_url + "/status", timeout=10))
    if status.get("sessionId"):
        return status["sessionId"]
    request = urllib.request.Request(wda_url + "/session", method="POST",
                                     data=json.dumps({"capabilities": {"alwaysMatch": {}}}).encode(),
                                     headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=30))["sessionId"]


def run_task(task, *, repo, udid, manifest, wda_url, mjpeg_url, folder, policy="mobster", frontier_model=None,
             reasoning="low"):
    from ..agent import Agent
    from ..compose import build_models, build_target_driver, build_vision_judge, close_all
    from ..frame_clock import attach_frame_clock
    from ..gemini import configured as helper_configured

    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    t_reset = time.monotonic()
    reset(repo, udid, manifest)
    reset_s = time.monotonic() - t_reset

    bundles = [app_entry(manifest, app).get("bundle_id") for app in task.get("apps", [])]
    bundles = [b for b in bundles if b]
    costs, events = [], []

    def emit(event):
        kind = event.get("event")
        if kind == "frontier_prompt":
            # Kept out of task.json: prompts.jsonl, one line per turn, and the image the model saw as
            # screens/prompt-turn-NN.jpg, so a decision can be replayed offline (bench/replay.py).
            event = dict(event)
            image = event.pop("image", None)
            if image and image.startswith("data:image/jpeg;base64,"):
                name = f"prompt-turn-{event.get('step', 0):02d}.jpg"
                (folder / "screens").mkdir(parents=True, exist_ok=True)
                (folder / "screens" / name).write_bytes(base64.b64decode(image.split(",", 1)[1]))
                event["image"] = f"screens/{name}"
            with open(folder / "prompts.jsonl", "a", encoding="utf-8") as prompts:
                prompts.write(json.dumps(event, ensure_ascii=False) + "\n")
            return
        if kind == "inference_finished" and isinstance(event.get("cost_nanodollars"), int):
            costs.append(event["cost_nanodollars"] / 1e9)
        if kind in ("frontier_decision", "frontier_error", "frontier_refused", "frontier_action", "frontier_failed",
                    "frontier_chunk_stop", "frontier_checklist", "frontier_checklist_update", "frontier_verify"):
            event = {k: v for k, v in event.items() if k != "usage"}
            trace = getattr(driver, "call_trace", None)
            if trace and kind in ("frontier_decision", "frontier_action", "frontier_failed"):
                # Every WDA request, read and judge screenshot since the last event: [kind, start ms, ms].
                items = trace[:]
                del trace[:len(items)]  # the verify read's thread may append meanwhile
                event["wda"] = [[name, round((at - started) * 1000), round(ms)] for name, at, ms in items]
            events.append(event)
            if kind == "frontier_decision" and recording is not None:
                recording.turn = event.get("step")
            return
        if kind in ("decision", "result", "plan_compiled", "loop_compiled", "route_decision", "step_interrupted",
                    "action_not_dispatched", "stale_decision", "observation", "action_check", "helper",
                    "extraction_selection_unsure", "completion_continued", "stop_gate_unclear_read_only", "error",
                    "milestones_compiled", "milestones_not_compiled", "unclear_requested_allowed",
                    "stop_gate_unfounded"):
            kept = {k: v for k, v in event.items() if isinstance(v, (str, int, float, bool)) or v is None}
            if isinstance(kept.get("text"), str):
                kept["text"] = kept["text"][:600]
            events.append(kept)

    shots = Shots(udid, folder / "screens")
    driver = model = helper = recording = calls = None
    model_name = (frontier_model or DEFAULT_FRONTIER_MODEL) if policy == "frontier" else None
    os.environ["MOBSTER_FRAME_CLOCK"] = "on"
    started = time.monotonic()
    try:
        session = wda_session(wda_url)
        driver = build_target_driver(wda_url=wda_url, session=session)
        driver.call_trace = shots.trace = []
        attach_frame_clock(driver, wda_url=wda_url, session=session, mode="on", mjpeg_url=mjpeg_url,
                           log_path=str(folder / "frameclock.jsonl"))
        clear_alerts(driver)
        driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
        recording = RecordingDriver(driver, shots)
        if bundles:
            recording.call("POST", "/wda/apps/activate", {"bundleId": bundles[0]}, timeout=20)
        if policy == "frontier":
            from ..catalog import app_label
            from ..frontier import FrontierAgent, chat_client, cost_usd
            client = chat_client(model_name, reasoning=reasoning)
            names = {b: app_label(b).split(" (")[0] for b in bundles}
            started, shot_seconds = time.monotonic(), shots.seconds
            outcome = FrontierAgent(recording, client, apps=names, emit=emit, max_steps=MAX_STEPS,
                                    max_seconds=MAX_SECONDS, max_cost_usd=task_cost_cap(bundles)).run(task["goal"])
            costs.append(cost_usd(client.model, outcome["usage"]) or 0.0)
            calls = outcome["usage"].get("calls")  # FrontierAgent emits no inference_finished: one cost, many calls
            result = {"status": outcome["status"], "data": outcome.get("answer"),
                      "reason": outcome.get("reason", ""), "actions": outcome["actions"],
                      "decisions": outcome["steps"], "usage": outcome["usage"], "notes": outcome["notes"],
                      "timing": judge_shots_apart(outcome.get("timing"), shots.seconds - shot_seconds)}
        else:
            model, helper = build_models(helper=helper_configured(), on_inference=emit)
            options = {"vision_judge": build_vision_judge(enabled=helper is not None, on_inference=emit)}
            if len(bundles) > 1:
                options["allowed_bundles"] = frozenset(bundles)
            started = time.monotonic()
            agent = Agent(recording, model, helper, emit=emit, max_steps=MAX_STEPS, max_seconds=MAX_SECONDS,
                          **options)
            result = agent.run(task["goal"], execute=True, output_format="auto")
    except Exception as error:
        result = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
    finally:
        elapsed = time.monotonic() - started
        close_all(model, helper, driver)
    steps = recording.steps if recording is not None else []
    final = shots.take("final")
    if steps:
        steps[-1]["post_action_screenshot"] = final
    else:
        steps = [{"step": 1, "screenshot": final, "actions": [], "post_action_screenshot": final}]
    answer = answer_text(result)
    if answer is not None:
        steps.append({"step": len(steps) + 1, "actions": [{"type": "stop", "answer": answer}],
                      "post_action_screenshot": final})
    (folder / "trajectory.json").write_text(json.dumps(steps, indent=1), encoding="utf-8")
    # "ok" means the agent ran to a result; iOSWorld's judge then decides success.
    record = {"task": task["name"], "goal": task["goal"], "apps": task.get("apps", []),
              "category": task.get("category"), "difficulty": task.get("difficulty"),
              "status": "ok", "agent_answer": answer,
              "mobster": {"status": result.get("status"), "reason": str(result.get("reason") or "")[:400],
                          "data_status": result.get("data_status"), "actions": result.get("actions"),
                          "decisions": result.get("decisions"),
                          "agent_seconds": round(max(0.0, elapsed - shots.seconds), 2),
                          "screenshot_seconds": round(shots.seconds, 2), "reset_seconds": round(reset_s, 1),
                          "cost_usd": round(sum(costs), 6),
                          "model_calls": calls if calls is not None else len(costs), "policy": policy,
                          "model": model_name, "usage": result.get("usage"),
                          "notes": result.get("notes"), "timing": result.get("timing"),
                          "events": events[-400:]}}
    (folder / "task.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


# -- the run, the judge, the report -------------------------------------------------------

def load_envs(paths):
    from ..config import load_env_file
    for path in paths or ():
        load_env_file(os.path.expanduser(path))


def cmd_run(args):
    # OPENAI_API_KEY goes into the apps' UserDefaults, as iOSWorld's runner does (its apps' in-app replies).
    load_envs(args.env_file)
    tasks = all_tasks = load_tasks(args.repo)
    all_names = [t["name"] for t in all_tasks]  # folder numbers follow the full task list
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["name"] in wanted]
    if args.limit:
        tasks = tasks[:args.limit]
    manifest = load_manifest(args.repo)
    register_names(manifest)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        folder = out / f"{all_names.index(task['name']) + 1:0{TASK_DIR_WIDTH}d}-{task['name']}"
        if (folder / "task.json").exists() and not args.force:
            print(f"skip {task['name']} (done)", flush=True)
            continue
        try:
            record = run_task(task, repo=args.repo, udid=args.udid, manifest=manifest, wda_url=args.wda_url,
                              mjpeg_url=args.mjpeg_url, folder=folder, policy=args.policy, frontier_model=args.model,
                              reasoning=args.reasoning)
        except RuntimeError as error:
            # A failed reset is the harness's, not the agent's: no task.json, so a rerun retries it,
            # and the rest of this simulator's queue still runs.
            print(f"{task['name']:24} NOT RUN (reset failed: {str(error)[:120]})", flush=True)
            continue
        m = record["mobster"]
        print(f"{task['name']:24} {m['status']:24} {m['agent_seconds']:6.1f}s ${m['cost_usd']:.4f} "
              f"steps={m['decisions']} answer={str(record['agent_answer'])[:80]!r}", flush=True)


def cmd_pool(args):
    """One ``run`` per simulator (UDID:WDA-port:MJPEG-port), the tasks dealt round-robin."""
    tasks = [t["name"] for t in load_tasks(args.repo)]
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t in wanted]
    if args.limit:
        tasks = tasks[:args.limit]
    workers = [spec.split(":") for spec in args.sims.split(",")]
    procs = []
    for index, (udid, port, mjpeg) in enumerate(workers):
        share = tasks[index::len(workers)]
        if not share:
            continue
        command = [sys.executable, "-u", "-m", "mobile_agent.bench.iosworld", "run", "--repo", args.repo,
                   "--udid", udid, "--wda-url", f"http://127.0.0.1:{port}", "--mjpeg-url", f"http://127.0.0.1:{mjpeg}",
                   "--out", args.out, "--only", ",".join(share)]
        for env_file in args.env_file or ():
            command += ["--env-file", env_file]
        command += ["--policy", args.policy, "--reasoning", args.reasoning] + (["--model", args.model] if args.model else [])
        if args.force:
            command.append("--force")
        log = open(pathlib.Path(args.out).with_suffix(f".worker{index}.log"), "w")
        procs.append((subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT), log))
    for proc, log in procs:
        proc.wait()
        log.close()
    return max((proc.returncode for proc, _ in procs), default=0)


def cmd_judge(args):
    """iOSWorld's own judge, unmodified, in its own environment."""
    load_envs(args.env_file)  # the judge reads OPENAI_API_KEY from this process's environment
    python = pathlib.Path(args.repo) / ".venv" / "bin" / "python"
    command = [str(python if python.exists() else sys.executable), "scripts/judge_trajectories.py",
               "--run-dir", str(pathlib.Path(args.run).resolve())]
    if args.force:
        command.append("--force")
    return subprocess.call(command, cwd=args.repo, env=dict(os.environ))


def cmd_report(args):
    rows = []
    for task_json in sorted(pathlib.Path(args.run).glob("*/task.json")):
        rows.append(json.loads(task_json.read_text(encoding="utf-8")))
    judged = [r for r in rows if r.get("evaluation")]
    ok = [r for r in judged if r["evaluation"].get("success")]
    print(f"tasks run {len(rows)}, judged {len(judged)}, success {len(ok)}"
          + (f" ({100 * len(ok) / len(judged):.1f}%)" if judged else ""))
    if judged:
        print(f"mean rubric score {statistics.mean(r['evaluation'].get('score', 0) for r in judged):.3f}")
    for key in ("category", "difficulty"):
        groups = {}
        for r in judged:
            groups.setdefault(r.get(key), []).append(bool(r["evaluation"].get("success")))
        print(key + ": " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(groups.items(), key=str)))
    from ..frontier import cost_usd
    seconds = [r["mobster"]["agent_seconds"] for r in rows]
    costs = [r["mobster"]["cost_usd"] or cost_usd(r["mobster"].get("model"), r["mobster"].get("usage")) or 0.0
             for r in rows]
    if seconds:
        print(f"agent seconds p50 {statistics.median(seconds):.1f}, mean {statistics.mean(seconds):.1f}; "
              f"$/task mean {statistics.mean(costs):.4f}, total ${sum(costs):.2f}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--udid", required=True)
    run.add_argument("--wda-url", required=True)
    run.add_argument("--mjpeg-url")
    run.add_argument("--out", required=True)
    run.add_argument("--only")
    run.add_argument("--limit", type=int)
    run.add_argument("--force", action="store_true")
    run.add_argument("--env-file", action="append")
    run.add_argument("--policy", choices=("mobster", "frontier"), default="mobster")
    run.add_argument("--model")
    run.add_argument("--reasoning", default="low")
    pool = sub.add_parser("pool")
    pool.add_argument("--repo", required=True)
    pool.add_argument("--sims", required=True, help="UDID:WDA_PORT:MJPEG_PORT,...")
    pool.add_argument("--out", required=True)
    pool.add_argument("--only")
    pool.add_argument("--limit", type=int)
    pool.add_argument("--force", action="store_true")
    pool.add_argument("--env-file", action="append")
    pool.add_argument("--policy", choices=("mobster", "frontier"), default="mobster")
    pool.add_argument("--model")
    pool.add_argument("--reasoning", default="low")
    judge = sub.add_parser("judge")
    judge.add_argument("--repo", required=True)
    judge.add_argument("--run", required=True)
    judge.add_argument("--force", action="store_true")
    judge.add_argument("--env-file", action="append")
    report = sub.add_parser("report")
    report.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    return {"run": cmd_run, "pool": cmd_pool, "judge": cmd_judge, "report": cmd_report}[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())

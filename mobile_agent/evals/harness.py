"""Run the on-device suite and grade it independently.

Reliability is the measurement, so every task runs ``--repeats`` times and the
reported number is the pass RATE. A suite that passed once is not a suite that
passes. Failures are attributed to the specific oracle that rejected the run, so
a regression names the property it broke rather than a task id.

Usage:
  mobile_agent/.venv/bin/python -m mobile_agent.evals.harness \
      --wda-url http://127.0.0.1:8100 --device iphone15pro --repeats 3
"""

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid

from ..extensions import load as load_extensions
from .oracles import ProbeUnavailable, WDAProbe
from .tasks import DEVICES, suite


class Client:
    def __init__(self, base, timeout=30):
        self.base, self.timeout = base.rstrip("/"), timeout

    def _call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def status(self):
        return self._call("GET", "/api/status")

    def submit(self, task):
        body = {"appId": task.app_id, "goal": task.goal, "mode": "live",
                "outputFormat": task.output_format}
        if task.output_schema is not None:
            body["outputSchema"] = task.output_schema
        response = self._call("POST", "/api/runs", body,
                              {"Idempotency-Key": f"eval-{task.id}-{uuid.uuid4().hex[:12]}"})
        return response["run"]["id"]

    def wait(self, run_id, deadline):
        """Poll to a terminal state. A run still going at the deadline is a failure."""
        while time.monotonic() < deadline:
            try:
                run = self._call("GET", f"/api/runs/{run_id}").get("run")
            except (urllib.error.URLError, json.JSONDecodeError, KeyError):
                time.sleep(1)
                continue
            if run and run.get("status") not in ("running", "queued"):
                return run
            # Polling granularity is added to every measured time; keep it small.
            time.sleep(.1)
        return None


class InProcess:
    """Run the agent in this process against a USB iPhone, exactly as ``serve`` does.

    For measuring the agent without a server: it takes the same exclusive
    device lease a server run takes (so it never drives the phone at the same
    time as a server task), resolves WDA's current session, activates the
    task's app, and runs ``Agent`` with the same "In <App>: <goal>" framing.
    Each record also carries model calls by purpose and per-phase timings.
    """

    def __init__(self, wda_url, *, env_file=None, replay_dir=None, helper=True, memo_dir=None, lease=True):
        from ..config import load_env_file
        if env_file:
            load_env_file(env_file)
        self.wda_url = wda_url.rstrip("/")
        self.replay_dir = replay_dir
        self.helper = helper
        # False when a DevicePool already holds this phone's lease (--devices).
        self.lease = lease
        # Exact decision memo: None (cold runs, the default), or a directory.
        from ..decision_memo import DecisionMemo
        self.memo = DecisionMemo(memo_dir) if memo_dir else None

    def status(self):
        from ..drivers import resolve_wda_session
        try:
            ready = bool(resolve_wda_session(self.wda_url, create=False))
        except Exception:
            ready = False
        return {"device_ready": ready, "live_enabled": ready, "limitations": [] if ready else ["WDA has no session"]}

    def run_task(self, task, deadline):
        from ..agent import Agent
        from ..catalog import APPS
        from ..compose import build_models, build_target_driver, close_all, warm_clients
        from ..drivers import resolve_wda_session
        from ..gemini import configured as helper_configured
        from ..journal import Lease
        from ..latency_trace import Trace
        from ..replay import ReplayStore

        app = next(a for a in APPS if a["id"] == task.app_id)
        events = []
        trace = Trace()
        lease = Lease.device(self.wda_url) if self.lease else None
        driver = model = helper = None
        try:
            # Model connections open while the phone is prepared (as in serve).
            with trace.span("setup.models"):
                model, helper = build_models(helper=self.helper and helper_configured(),
                                             on_inference=events.append)
                warm_clients(model, helper)
            with trace.span("setup.driver"):
                session = resolve_wda_session(self.wda_url)
                driver = build_target_driver(wda_url=self.wda_url, session=session)
                # Pixel settle (MOBSTER_FRAME_CLOCK) over its own MJPEG connection; the
                # driver's close releases it.
                from ..frame_clock import attach_frame_clock
                attach_frame_clock(driver, wda_url=self.wda_url, session=session)
            with trace.span("launch.activate"):
                driver.call("POST", "/wda/apps/activate", {"bundleId": app["bundleId"]})
            agent = Agent(driver, model, helper, emit=events.append,
                          max_seconds=max(1, deadline - time.monotonic()),
                          replay_store=ReplayStore(self.replay_dir) if self.replay_dir else None,
                          trace=trace, launch_bundle=app["bundleId"], decision_memo=self.memo)
            result = agent.run(f"In {app['name']}: {task.goal}", execute=True,
                               output_schema=task.output_schema, output_format=task.output_format)
        except Exception as error:
            result = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
        finally:
            with trace.span("finish.close"):
                close_all(model, helper, driver)
            if lease is not None:
                lease.close()
            events.append(trace.event())
        return {"status": result.get("status"), "summary": result, "metrics": run_metrics(events)}


SPEED_PATHS = ("frame_clock", "glide", "speculation", "same_screen_completion", "text_selection",
               "early_answer", "unguarded_scrolls", "pixel_guard", "prediction", "direct_url",
               "first_screen", "hedging", "fresh_guard", "speculative_answer")


def disable_speed_paths(names=SPEED_PATHS):
    """Turn off speed paths for same-day A/B runs in this process only (all of them:
    the pre-2026-09-23 loop). Single names bisect a regression to one feature."""
    import os
    from .. import agent, models
    unknown = set(names) - set(SPEED_PATHS)
    if unknown:
        raise SystemExit(f"Unknown speed paths: {sorted(unknown)}; choose from {', '.join(SPEED_PATHS)}")
    off = set(names)
    if "frame_clock" in off:
        os.environ["MOBSTER_FRAME_CLOCK"] = "off"  # pixel settle (frame_clock.py)
    if "glide" in off:
        os.environ["MOBSTER_GLIDE"] = "0"          # glide scrolls (drivers.WDA_GLIDE_SEGMENTS)
    if "speculation" in off:
        models.Jev.speculative_decisions = False
    if "same_screen_completion" in off:
        models.Jev.stable_completion = False
    if "text_selection" in off:
        models.request_text_candidates = lambda request, limit=None: []
    if "early_answer" in off:
        agent.Agent.early_answer = False
    if "unguarded_scrolls" in off:
        agent.UNGUARDED_OPERATIONS = frozenset()
    if "pixel_guard" in off:
        agent.Agent._pixels_still_since = lambda self, snapshot: False
    if "prediction" in off:
        agent.predicted_screen = lambda snapshot, operation, target: None
    if "direct_url" in off:
        agent.requested_url = lambda goal: None
    if "first_screen" in off:
        agent.first_screen_candidates = lambda bundle: []
    if "hedging" in off:
        models.Jev.hedging = False
        models.Helper.hedging = False
    if "fresh_guard" in off:
        agent.FRESH_GUARD_SECONDS = -1.0
    if "speculative_answer" in off:
        agent.Agent._speculate_answer = lambda self, *args, **kwargs: None


def run_metrics(events):
    """Model calls by purpose and per-phase timings, from one run's events. Numbers only."""
    calls, model_ms, act, settle = {}, {}, {}, {}
    operation = None
    for event in events:
        kind = event.get("event")
        if kind == "inference_finished":
            purpose = f"{event.get('provider')}:{event.get('purpose')}"
            calls[purpose] = calls.get(purpose, 0) + 1
            model_ms[purpose] = round(model_ms.get(purpose, 0) + (event.get("latency_ms") or 0))
        elif kind == "action_started":
            operation = event.get("operation")
        elif kind == "action_acknowledged" and operation:
            act.setdefault(operation, []).append(round(event.get("act_ms") or 0))
        elif kind == "observation_after_action" and operation:
            settle.setdefault(operation, []).append(round(event.get("settle_ms") or 0))
    counted = ("decision_speculated", "decision_speculation_used", "decision_speculation_discarded",
               "decision_memo_hit", "decision_first_screen_predicted", "completion_same_screen", "text_selected", "replay_abandoned", "stale_decision",
               "decision_predicted", "refresh_skipped_pixels_still", "loop_item", "loop_escalation")
    # Numbers and digests only, never answer or screen text: what calibrate.py needs.
    signals = [{key: value for key, value in event.items() if key != "event" and key != "seq"
                and isinstance(value, (int, float, str, bool, type(None)))}
               for event in events if event.get("event") == "answer_signals"]
    decisions = [{"operation": event.get("operation"), "confidence": event.get("confidence"),
                  "demoted_from": event.get("demoted_from")}
                 for event in events if event.get("event") == "decision"]
    trace = next((event for event in events if event.get("event") == "latency_trace"), None)
    return {"model_calls": calls, "model_ms": model_ms, "act_ms": act, "settle_ms": settle,
            "answer_signals": signals, "decisions": decisions,
            # The run's latency trace (phases, calls, marks), for latency_report.
            **({"latency_trace": {key: trace[key] for key in ("wall_ms", "phases", "calls", "marks", "summary")
                                  if key in trace}} if trace else {}),
            **{name: sum(1 for event in events if event.get("event") == name) for name in counted}}


def grade(task, run, probe):
    """Every oracle runs, so one failure does not hide the others."""
    results = []
    for oracle in task.oracles:
        try:
            ok, detail = oracle.check(run, probe, task)
        except ProbeUnavailable as error:
            results.append({"oracle": oracle.name, "ok": False, "detail": f"probe unavailable: {error}",
                            "ungraded": True})
        except Exception as error:
            results.append({"oracle": oracle.name, "ok": False, "detail": f"{type(error).__name__}: {error}"})
        else:
            results.append({"oracle": oracle.name, "ok": bool(ok), "detail": detail})
    return results


def busy_runs(url):
    """Runs another Mobster server has queued or running on the phone (GET only)."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/runs", timeout=5) as response:
            runs = json.loads(response.read().decode()).get("runs", [])
    except (OSError, ValueError, AttributeError):
        return None  # Unknown; the caller waits as if busy.
    return [run for run in runs if isinstance(run, dict) and run.get("status") in ("queued", "running")]


def yield_to(url, *, wait_seconds=600):
    """Before touching the phone, wait until the other server has no active task."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        busy = busy_runs(url)
        if busy == []:
            return True
        time.sleep(5)
    return False


def attempt(task, client, probe, index, yield_url=None):
    record = {"task": task.id, "category": task.category, "attempt": index}
    if yield_url and not yield_to(yield_url):
        return {**record, "passed": False, "error": "skipped: another server kept the phone busy", "oracles": []}
    try:
        probe.reset(task.bundle_id, task.reset_url)
    except ProbeUnavailable as error:
        return {**record, "passed": False, "error": f"reset failed: {error}", "oracles": []}
    time.sleep(1.5)
    started = time.monotonic()
    if callable(getattr(client, "run_task", None)):
        run_id = None
        run = client.run_task(task, started + task.max_seconds)
    else:
        try:
            run_id = client.submit(task)
        except Exception as error:
            return {**record, "passed": False, "error": f"submit failed: {type(error).__name__}: {error}", "oracles": []}
        run = client.wait(run_id, started + task.max_seconds)
    elapsed = (time.monotonic() - started) * 1000
    if run is None:
        return {**record, "passed": False, "run_id": run_id, "elapsed_ms": elapsed,
                "error": "run did not reach a terminal state", "oracles": []}
    summary = run.get("summary") or {}
    oracles = grade(task, run, probe)
    return {**record, "run_id": run_id, "elapsed_ms": round(elapsed, 1),
            "status": run.get("status"), "data": summary.get("data"),
            "data_status": summary.get("data_status"), "actions": summary.get("actions"),
            "agent_ms": summary.get("elapsed_ms"), "decisions": summary.get("decisions"),
            "helper_calls": summary.get("helper_calls"), "reason": summary.get("reason"),
            **({"metrics": run["metrics"]} if run.get("metrics") else {}),
            "passed": all(item["ok"] for item in oracles) and bool(oracles),
            "oracles": oracles}


def run_parallel(args, tasks_for, only):
    """Independent attempts across several USB phones at once (``--devices``).

    One worker per phone (pool.run_on_devices); each attempt holds its phone
    exclusively and is graded against that phone's own ground truth, so a job
    only runs on a phone whose ``truth`` matches it.
    """
    from ..evals.oracles import WDAProbe
    from ..pool import DevicePool, run_on_devices, wda_specs_from_env

    specs = wda_specs_from_env(args.devices)
    graded = [spec for spec in specs if spec.truth in DEVICES]
    if not graded:
        raise SystemExit("--devices needs at least one phone with truth=<device> (see evals/tasks.DEVICES)")
    pool = DevicePool(specs)
    jobs = []
    for index in range(args.repeats):
        for truth in sorted({spec.truth for spec in graded}):
            for task in tasks_for(truth):
                if not only or any(part in task.id for part in only):
                    jobs.append((truth, task, index))
    clients, probes, lock = {}, {}, threading.Lock()

    def work(spec, job):
        truth, task, index = job
        with lock:
            client = clients.setdefault(spec.id, InProcess(spec.wda_url, env_file=args.env_file,
                                                           replay_dir=args.replay_dir, memo_dir=args.memo_dir,
                                                           lease=False))
            probe = probes.setdefault(spec.id, WDAProbe(spec.wda_url))
        return {**attempt(task, client, probe, index, args.yield_to), "device": spec.id}

    def show(job, spec, record):
        if isinstance(record, dict):
            mark = "PASS" if record["passed"] else "FAIL"
            print(f"  [{spec.id} {job[2] + 1}/{args.repeats}] {mark} {job[1].id} "
                  f"{record.get('elapsed_ms')} ms", flush=True)
        else:
            print(f"  [{getattr(spec, 'id', '-')}] ERROR {job[1].id}: {type(record).__name__}: {record}", flush=True)
    started = time.monotonic()
    results = run_on_devices(pool, jobs, work, eligible=lambda spec, job: spec.truth == job[0], on_result=show)
    records = []
    for job, spec, record in results:
        if isinstance(record, dict):
            records.append(record)
        else:
            records.append({"task": job[1].id, "category": job[1].category, "attempt": job[2], "passed": False,
                            "error": f"{type(record).__name__}: {record}", "oracles": [],
                            "device": getattr(spec, "id", None)})
    wall = time.monotonic() - started
    print(f"\n{len(records)} attempts on {len(graded)} phone(s) in {wall:.0f} s "
          f"({3600 * len(records) / max(wall, 1e-9):.0f} attempts/hour)")
    return records


def report(records, repeats):
    by_task = {}
    for record in records:
        by_task.setdefault(record["task"], []).append(record)
    print(f"\n{'TASK':34} {'CATEGORY':11} {'PASS':>7}  {'p50 ms':>8}  {'agent ms':>8}  FAILING ORACLE")
    print("-" * 114)
    total_passed = 0
    for task_id, attempts in by_task.items():
        passed = sum(1 for a in attempts if a["passed"])
        total_passed += passed
        times = [a["elapsed_ms"] for a in attempts if a.get("elapsed_ms")]
        agent_times = [a["agent_ms"] for a in attempts if a.get("agent_ms")]
        failing = sorted({item["oracle"] for a in attempts for item in a["oracles"] if not item["ok"]})
        note = ", ".join(failing) if failing else ""
        if not failing and any(a.get("error") for a in attempts):
            note = next(a["error"] for a in attempts if a.get("error"))
        rate = f"{passed}/{len(attempts)}"
        p50 = f"{statistics.median(times):.0f}" if times else "-"
        agent = f"{statistics.median(agent_times):.0f}" if agent_times else "-"
        print(f"{task_id:34} {attempts[0]['category']:11} {rate:>7}  {p50:>8}  {agent:>8}  {note[:44]}")
    print("-" * 114)
    total = len(records)
    print(f"{'TOTAL':34} {'':11} {total_passed}/{total}"
          f"  ({100 * total_passed / total:.0f}% of attempts passed every oracle)\n")
    by_category = {}
    for record in records:
        hit, seen = by_category.setdefault(record["category"], [0, 0])
        by_category[record["category"]] = [hit + int(record["passed"]), seen + 1]
    for category, (hit, seen) in sorted(by_category.items()):
        print(f"  {category:12} {hit}/{seen}")
    return total_passed == total


def main():
    hooks = load_extensions()
    parser = argparse.ArgumentParser(description="On-device task evaluation with independent oracles")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--wda-url", help="WebDriverAgent on a USB iPhone (e.g. via iproxy)")
    for extension in hooks.eval_targets:
        extension.add_arguments(target)
    parser.add_argument("--session", help="WDA session; defaults to the one WDA reports")
    parser.add_argument("--device", choices=sorted(DEVICES),
                        help="Whose ground truth to grade against (default: iphone15pro with --wda-url)")
    parser.add_argument("--api", default="http://127.0.0.1:8765", help="A running mobster serve")
    parser.add_argument("--in-process", action="store_true",
                        help="Run the agent in this process against --wda-url instead of through --api")
    parser.add_argument("--env-file", help="Env file with model keys (in-process runs)")
    parser.add_argument("--yield-to", help="Another Mobster server (e.g. http://127.0.0.1:8765): before each "
                        "attempt, wait until it has no queued or running task (GET only)")
    parser.add_argument("--replay-dir", help="Compiled-run store for in-process runs (default: none, cold runs)")
    parser.add_argument("--memo-dir", help="Decision memo for in-process runs (default: none, cold runs)")
    parser.add_argument("--devices", help="Several USB phones at once (in-process): MOBSTER_WDA_DEVICES syntax, "
                        "e.g. 'id=15pro,wda=http://127.0.0.1:8100,truth=iphone15pro;id=xr,wda=http://127.0.0.1:8101'")
    parser.add_argument("--baseline", action="store_true",
                        help="In-process A/B: disable text selection, speculative decisions and "
                             "same-screen completion (the 2026-09-22 behaviour)")
    parser.add_argument("--disable", help="Comma list of speed paths to turn off (bisecting): " + ", ".join(SPEED_PATHS))
    parser.add_argument("--suite", default="core", help="all, or a comma list of task suites (see tasks.SUITES)")
    parser.add_argument("--repeats", type=int, default=3, help="Attempts per task; reliability needs more than one")
    parser.add_argument("--only", help="Substring filter on task id")
    parser.add_argument("--out", help="Write per-attempt JSONL here")
    args = parser.parse_args()

    # An extension's target, when one was chosen: (probe, default ground truth).
    other = next((chosen for extension in hooks.eval_targets
                  if (chosen := extension.probe(args)) is not None), None)
    if not (args.wda_url or args.devices or other):
        parser.error("one of --wda-url or --devices is required")
    if args.devices:
        if not args.in_process:
            raise SystemExit("--devices runs in-process; add --in-process")
        if args.baseline:
            disable_speed_paths()
        only = [part for part in (args.only or "").split(",") if part]
        records = run_parallel(args, lambda truth: suite(truth, args.suite), only)
        if args.out:
            with open(args.out, "w") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
        raise SystemExit(0 if report(records, args.repeats) else 1)
    if args.in_process and not args.wda_url:
        raise SystemExit("--in-process needs --wda-url")
    if args.baseline:
        if not args.in_process:
            raise SystemExit("--baseline needs --in-process")
        disable_speed_paths()
    elif args.disable:
        if not args.in_process:
            raise SystemExit("--disable needs --in-process")
        disable_speed_paths([name.strip() for name in args.disable.split(",") if name.strip()])
    client = (InProcess(args.wda_url, env_file=args.env_file, replay_dir=args.replay_dir, memo_dir=args.memo_dir)
              if args.in_process else Client(args.api))
    status = client.status()
    if not status.get("device_ready") or not status.get("live_enabled"):
        raise SystemExit(f"Device is not ready for live tasks: {status.get('limitations')}")
    probe, default_truth = (WDAProbe(args.wda_url, args.session), "iphone15pro") if args.wda_url else other
    device = args.device or default_truth

    only = [part for part in (args.only or "").split(",") if part]
    tasks = [task for task in suite(device, args.suite) if not only or any(part in task.id for part in only)]
    if not tasks:
        raise SystemExit("No tasks matched")
    print(f"{len(tasks)} tasks x {args.repeats} attempts against {args.api} ({device} ground truth)")

    records = []
    for index in range(args.repeats):
        for task in tasks:
            record = attempt(task, client, probe, index, args.yield_to)
            records.append(record)
            mark = "PASS" if record["passed"] else "FAIL"
            detail = "" if record["passed"] else " :: " + "; ".join(
                f"{item['oracle']} {item['detail']}" for item in record["oracles"] if not item["ok"])
            print(f"  [{index + 1}/{args.repeats}] {mark} {task.id}"
                  f" actions={record.get('actions')} {record.get('data')!r}{detail[:150]}")
    if args.out:
        with open(args.out, "w") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        print(f"\nPer-attempt records: {args.out}")
    raise SystemExit(0 if report(records, args.repeats) else 1)


if __name__ == "__main__":
    main()

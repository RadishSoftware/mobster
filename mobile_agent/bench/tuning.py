"""The engine tuning loop on iOSWorld: frozen splits, a ledger, paired ABBA runs, traces, a scoreboard.

Every engine change is a hypothesis drawn from measured failures, checked offline, then paired against
main on simulators. This module is the loop's harness; ``bench/iosworld.py`` runs one task.

    python -m mobile_agent.bench.tuning run --batch DIR --arm A=WORKTREE --arm B=WORKTREE \\
        --splits splits.json --split dev_core --runs 2 --sims UDID:WDA:MJPEG,... --repo ../iOSWorld \\
        --env-file bench.env --model claude-sonnet-5-5 --xctestrun WDA.xctestrun --boot --shutdown \\
        --ledger ledger.jsonl
    python -m mobile_agent.bench.tuning judge --batch DIR --repo ../iOSWorld --env-file bench.env --ledger ...
    python -m mobile_agent.bench.tuning trace --batch DIR          per-step trace.jsonl in every task folder
    python -m mobile_agent.bench.tuning scoreboard --batch DIR [--labels labels.jsonl] [--ledger ...]
    python -m mobile_agent.bench.tuning label --batch DIR --model claude-sonnet-5-5 --env-file bench.env
    python -m mobile_agent.bench.tuning counterfactual --runs DIR... --variant pkg.mod:fn --model ... --n 40

**Runs.** Each task gets 2 runs per arm, interleaved by task as A B B A, and the four runs of a task are
dealt to the simulators with a rotation, so every simulator runs every arm equally often. A batch folder
holds one run folder per arm and run (``A1``, ``B1``, ``B2``, ``A2``), each as ``bench/iosworld.py`` writes
it, so ``iosworld compare --a DIR/A* --b DIR/B*`` reads it as it is.

**No driver outlives its run.** Each task runs as a child in its own session with a hard timeout; when it
passes, the child's whole group is killed. A reaper in its own session watches this process: when it ends
(normally, killed, or its terminal closed) the reaper kills any child still running, stops the batch's WDA
runners and, with ``--shutdown``, shuts the simulators down. Nothing is left re-parented to launchd.

**The ledger** (JSONL) gets one line per attempt (``kind: run``) and one per judged attempt (``kind:
judged``): task, split, build, arm, run, simulator, status, seconds, $ and calls, then the official pass and
the rubric. The splits file is refused if its hash does not match its frozen ``sha256``.
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import pathlib
import random
import re
import signal
import subprocess
import sys
import threading
import time

ARM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
TASK_TIMEOUT = 1200  # iosworld's agent stops itself at 900 s; past this the harness is hung
REAP_POLL = 1.0
KILL_GRACE = 5.0


# -- splits and ledger -------------------------------------------------------------------------

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def load_splits(path):
    """The frozen splits document; refuses one whose ``splits`` no longer hash to its ``sha256``."""
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    digest = hashlib.sha256(canonical(doc["splits"])).hexdigest()
    if doc.get("sha256") and doc["sha256"] != digest:
        raise SystemExit(f"splits file {path} changed after it was frozen ({digest[:12]} != {doc['sha256'][:12]})")
    return doc


def split_of(doc, task):
    """The task's split (dev, val or test; dev_core is a subset of dev)."""
    for name in ("dev", "val", "test"):
        if task in doc["splits"].get(name, ()):
            return name
    return "unknown"


def append_ledger(path, record, lock=threading.Lock()):
    if not path:
        return
    record = {"at": datetime.datetime.now().isoformat(timespec="seconds"), **record}
    with lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_ledger(path):
    rows = []
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


# -- the ABBA schedule ---------------------------------------------------------------------------

def abba(arms, runs):
    """The order of one task's attempts: A B B A for two arms and two runs, generally the arms forward
    on odd runs and backward on even ones. Returns [(arm, run)]."""
    order = []
    for run in range(1, runs + 1):
        order += [(arm, run) for arm in (arms if run % 2 else list(reversed(arms)))]
    return order


def schedule(tasks, arms, runs, n_sims, seed=None):
    """Per-simulator job queues [(task, arm, run)]: tasks in a seeded order, each task's attempts in ABBA
    order, attempt k of the i-th task on simulator (k + i) mod n, so every simulator runs every arm alike."""
    tasks = list(tasks)
    if seed is not None:
        random.Random(seed).shuffle(tasks)
    queues = [[] for _ in range(n_sims)]
    for i, task in enumerate(tasks):
        for k, (arm, run) in enumerate(abba(arms, runs)):
            queues[(k + i) % n_sims].append((task, arm, run))
    return queues


# -- children that die with the run ---------------------------------------------------------------

def kill_group(pid, grace=KILL_GRACE):
    """SIGTERM a child's process group, then SIGKILL what is left after ``grace`` seconds."""
    for number, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, number)
        except OSError:
            return
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(.2)


def run_child(command, *, cwd, env, log_path, timeout, active, on_change=None, clock=time.monotonic):
    """Run ``command`` in its own session; kill its group past ``timeout``. Returns (exit code, timed out).
    ``active`` is the set of live child pids the reaper reads; ``on_change`` is called when it changes."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    active.add(proc.pid)
    if on_change:
        on_change()
    try:
        end = clock() + timeout
        while proc.poll() is None:
            if clock() > end:
                kill_group(proc.pid)
                proc.wait()
                return proc.returncode, True
            time.sleep(.5)
        kill_group(proc.pid, grace=1)  # anything the child left in its group goes with it
        return proc.returncode, False
    finally:
        active.discard(proc.pid)
        if on_change:
            on_change()


def write_active(path, active, lock=threading.Lock()):
    with lock:
        tmp = pathlib.Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(sorted(active)))
        os.replace(tmp, path)


def start_reaper(batch, udids, shutdown, python=sys.executable):
    """The reaper: its own session, watching this process (see the module docstring)."""
    command = [python, "-m", "mobile_agent.bench.tuning", "reap", "--parent", str(os.getpid()),
               "--active", str(pathlib.Path(batch) / ".active.json"), "--udids", ",".join(udids)]
    if shutdown:
        command.append("--shutdown")
    root = str(pathlib.Path(__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": root}
    log = open(pathlib.Path(batch) / "reaper.log", "ab")
    try:
        return subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        log.close()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_runners(udids):
    try:
        from .sim_wda import runner_pids, _signal
    except ImportError:  # the open-source tree starts no simulator runners, so it has none to stop
        return
    for udid in udids:
        pids = runner_pids(udid)
        for number in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                _signal(pid, number)
            time.sleep(1)
            pids = [p for p in pids if alive(p)]
            if not pids:
                break


def shutdown_sims(udids):
    for udid in udids:
        subprocess.run(["xcrun", "simctl", "shutdown", udid], capture_output=True)


def cmd_reap(args):
    """Wait for --parent to end, then clean up after it (idempotent)."""
    while alive(args.parent):
        time.sleep(REAP_POLL)
    try:
        pids = json.loads(pathlib.Path(args.active).read_text())
    except (OSError, ValueError):
        pids = []
    for pid in pids:
        kill_group(pid, grace=2)
    udids = [u for u in args.udids.split(",") if u]
    stop_runners(udids)
    if args.shutdown:
        shutdown_sims(udids)
    print(f"{datetime.datetime.now():%H:%M:%S} reaped: {len(pids)} children, runners of {len(udids)} sims"
          + (", sims shut down" if args.shutdown else ""), flush=True)
    return 0


# -- simulators ---------------------------------------------------------------------------------------

def parse_sims(spec):
    sims = []
    for item in spec.split(","):
        udid, port, mjpeg = item.split(":")
        sims.append((udid, int(port), int(mjpeg)))
    if len({s[0] for s in sims}) < len(sims):
        raise SystemExit("--sims lists a simulator twice")
    return sims


def boot(sims, xctestrun, log_dir):
    """Boot each simulator headless (``simctl boot`` opens no window) and start its WDA runner."""
    from .sim_wda import SimulatorWDA, ready
    for udid, _, _ in sims:
        subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True)
    for udid, _, _ in sims:
        subprocess.run(["xcrun", "simctl", "bootstatus", udid, "-b"], capture_output=True, timeout=600)
    for udid, port, mjpeg in sims:
        if not ready(f"http://127.0.0.1:{port}"):
            SimulatorWDA(udid, port, mjpeg, xctestrun, log_path=str(pathlib.Path(log_dir) / f"wda-{port}.log")).start()
    end = time.monotonic() + 240
    while time.monotonic() < end and not all(ready(f"http://127.0.0.1:{p}") for _, p, _ in sims):
        time.sleep(2)
    missing = [p for _, p, _ in sims if not ready(f"http://127.0.0.1:{p}")]
    if missing:
        raise SystemExit(f"WDA did not come up on {missing}")


# -- the batch -----------------------------------------------------------------------------------------

def task_folder(out, task, names):
    from .iosworld import TASK_DIR_WIDTH
    return pathlib.Path(out) / f"{names.index(task) + 1:0{TASK_DIR_WIDTH}d}-{task}"


def job_command(arm_python, args, sim, out, task):
    udid, port, mjpeg = sim
    command = [arm_python, "-u", "-m", "mobile_agent.bench.iosworld", "run", "--repo", args.repo, "--udid", udid,
               "--wda-url", f"http://127.0.0.1:{port}", "--mjpeg-url", f"http://127.0.0.1:{mjpeg}",
               "--out", str(out), "--only", task, "--policy", "frontier"]
    for env_file in args.env_file or ():
        command += ["--env-file", env_file]
    if args.model:
        command += ["--model", args.model]
    if args.reasoning:
        command += ["--reasoning", args.reasoning]
    if args.xctestrun:
        command += ["--wda-xctestrun", args.xctestrun]
    return command


def parse_arms(values):
    arms = collections.OrderedDict()
    for value in values:
        name, _, path = value.partition("=")
        if not ARM_NAME.match(name) or not path:
            raise SystemExit(f"--arm wants NAME=WORKTREE, got {value!r}")
        root = pathlib.Path(path).expanduser().resolve()
        if not (root / "mobile_agent" / "bench" / "iosworld.py").is_file():
            raise SystemExit(f"arm {name}: {root} is not a mobster-app worktree")
        arms[name] = root
    if len(arms) < 1:
        raise SystemExit("at least one --arm")
    return arms


def parse_arm_env(values, arms):
    """{arm: {KEY: VALUE}} from --arm-env ARM:KEY=VALUE (an arm's own environment: a switch under test)."""
    env = {name: {} for name in arms}
    for value in values or ():
        arm, _, assignment = value.partition(":")
        key, _, setting = assignment.partition("=")
        if arm not in arms or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key or ""):
            raise SystemExit(f"--arm-env wants ARM:KEY=VALUE for a named arm, got {value!r}")
        env[arm][key] = setting
    return env


def cmd_run(args):
    from .iosworld import build_id, check_isolation, load_manifest, load_tasks
    batch = pathlib.Path(args.batch).expanduser().resolve()
    batch.mkdir(parents=True, exist_ok=True)
    arms = parse_arms(args.arm)
    arm_env = parse_arm_env(args.arm_env, arms)
    sims = parse_sims(args.sims)
    all_names = [t["name"] for t in load_tasks(args.repo)]
    doc = load_splits(args.splits) if args.splits else None
    if args.only:
        tasks = args.only.split(",")
    elif doc and args.split:
        tasks = list(doc["splits"][args.split])
    else:
        raise SystemExit("name the tasks: --only, or --splits with --split")
    unknown = [t for t in tasks if t not in all_names]
    if unknown:
        raise SystemExit(f"not iOSWorld tasks: {unknown}")
    if doc and args.split != "test" and any(split_of(doc, t) == "test" for t in tasks):
        raise SystemExit("a test-split task outside --split test: test is opened only for a public claim")
    builds = {name: build_id(root / "mobile_agent" / "bench" / "iosworld.py") for name, root in arms.items()}
    meta = {"batch": batch.name, "arms": {n: str(p) for n, p in arms.items()}, "builds": builds, "arm_env": arm_env,
            "split": args.split, "splits_sha256": (doc or {}).get("sha256"), "tasks": tasks, "runs": args.runs,
            "model": args.model, "sims": [s[0] for s in sims], "seed": args.seed, "timeout": args.timeout,
            "started": datetime.datetime.now().isoformat(timespec="seconds")}
    (batch / "batch.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps({k: meta[k] for k in ("batch", "builds", "arm_env", "split", "runs", "model")}), flush=True)

    active = set()
    active_file = batch / ".active.json"
    write_active(active_file, active)
    reaper = start_reaper(batch, [s[0] for s in sims], args.shutdown)
    stopping = threading.Event()

    def on_signal(number, _frame):
        stopping.set()
        for pid in list(active):
            kill_group(pid, grace=2)
        raise SystemExit(128 + number)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        if args.boot:
            boot(sims, args.xctestrun, batch)
        manifest = load_manifest(args.repo)
        for udid, _, _ in sims:
            check_isolation(udid, manifest)
        queues = schedule(tasks, list(arms), args.runs, len(sims), seed=args.seed)
        headless = os.environ.get("MOBSTER_SHIMS")  # a folder of no-op `open`/`osascript`, first on PATH

        def worker(index):
            sim = sims[index]
            for task, arm, run in queues[index]:
                if stopping.is_set():
                    return
                out = batch / f"{arm}{run}"
                folder = task_folder(out, task, all_names)
                if (folder / "task.json").exists():
                    continue
                out.mkdir(parents=True, exist_ok=True)
                root = arms[arm]
                env = {**os.environ, "PYTHONPATH": str(root), "MOBSTER_BENCH_TTFT": args.ttft, **arm_env[arm]}
                if headless:
                    env["PATH"] = f"{headless}:{env.get('PATH', '')}"
                command = job_command(args.python or sys.executable, args, sim, out, task)
                started = time.monotonic()
                print(f"{datetime.datetime.now():%H:%M:%S} sim{index + 1} start {task} {arm}{run}", flush=True)
                code, timed_out = run_child(command, cwd=str(root), env=env, timeout=args.timeout, active=active,
                                            log_path=str(batch / f"{arm}{run}.sim{index + 1}.log"),
                                            on_change=lambda: write_active(active_file, active))
                seconds = round(time.monotonic() - started, 1)
                record = {"kind": "run", "batch": batch.name, "task": task,
                          "split": split_of(doc, task) if doc else None,
                          "in_split": args.split, "arm": arm, "run": run, "sim": sim[0], "build": builds[arm],
                          "model": args.model, "wall_s": seconds, "exit": "timeout" if timed_out else code,
                          "folder": str(folder)}
                try:
                    m = json.loads((folder / "task.json").read_text())["mobster"]
                    record.update({"status": m.get("status"), "agent_s": m.get("agent_seconds"),
                                   "usd": m.get("cost_usd"), "calls": m.get("model_calls"),
                                   "actions": m.get("actions"), "reason": (m.get("reason") or "")[:200]})
                except (OSError, ValueError, KeyError):
                    record["status"] = "infra"
                append_ledger(args.ledger, record)
                print(f"{datetime.datetime.now():%H:%M:%S} sim{index + 1} done  {task} {arm}{run} "
                      f"{record.get('status')} {record.get('agent_s')}s ${record.get('usd')}", flush=True)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(len(sims))]
        for thread in threads:
            thread.start()
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
    finally:
        stopping.set()
        for pid in list(active):
            kill_group(pid, grace=2)
        write_active(active_file, set())
        if args.shutdown:
            stop_runners([s[0] for s in sims])
            shutdown_sims([s[0] for s in sims])
        meta["ended"] = datetime.datetime.now().isoformat(timespec="seconds")
        (batch / "batch.json").write_text(json.dumps(meta, indent=1))
        print(f"{datetime.datetime.now():%H:%M:%S} batch ended", flush=True)
    del reaper  # it sees this process end and cleans up after it (idempotent)
    return 0


# -- judging ---------------------------------------------------------------------------------------

JUDGE_COST = re.compile(r"cost=\$([0-9.]+)")


def run_folders(batch):
    return sorted(p for p in pathlib.Path(batch).iterdir() if p.is_dir() and any(p.glob("*/task.json")))


def cmd_judge(args):
    """iOSWorld's own judge on every run folder of a batch, then one ``judged`` ledger line per attempt."""
    from .iosworld_stats import load_task
    batch = pathlib.Path(args.batch).expanduser().resolve()
    meta = json.loads((batch / "batch.json").read_text())
    doc = load_splits(args.splits) if args.splits else None
    total = 0.0
    for folder in run_folders(batch):
        log = batch / f"{folder.name}.judge.log"
        command = [sys.executable, "-m", "mobile_agent.bench.iosworld", "judge", "--repo", args.repo, "--run",
                   str(folder)]
        for env_file in args.env_file or ():
            command += ["--env-file", env_file]
        with open(log, "a") as handle:
            subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT,
                           env={**os.environ, "EVAL_MAX_WORKERS": str(args.workers)})
        spent = sum(float(x) for x in JUDGE_COST.findall(log.read_text(errors="replace")))
        total += spent
        arm, run = re.match(r"^(.*?)(\d+)$", folder.name).groups()
        for task_json in sorted(folder.glob("*/task.json")):
            r = load_task(str(task_json))
            append_ledger(args.ledger, {"kind": "judged", "batch": batch.name, "task": r.task,
                                        "split": split_of(doc, r.task) if doc else None, "arm": arm, "run": int(run),
                                        "build": meta["builds"].get(arm), "model": r.model, "judged": r.judged,
                                        "official": r.official, "rubric": r.score, "status": r.status,
                                        "agent_s": r.agent_seconds, "usd": r.cost_usd, "calls": r.calls,
                                        "folder": str(task_json.parent)})
    print(f"judge spend (logged): ${total:.4f}")
    (batch / "judge_cost.json").write_text(json.dumps({"usd": round(total, 4)}))
    return 0


# -- CLI ---------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.tuning", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="a paired ABBA batch on the simulators")
    run.add_argument("--batch", required=True)
    run.add_argument("--arm", action="append", required=True, help="NAME=WORKTREE (two for an A/B)")
    run.add_argument("--repo", required=True, help="the iOSWorld checkout")
    run.add_argument("--sims", required=True, help="UDID:WDA_PORT:MJPEG_PORT,...")
    run.add_argument("--splits")
    run.add_argument("--split")
    run.add_argument("--only")
    run.add_argument("--runs", type=int, default=2)
    run.add_argument("--seed", type=int, default=20261010)
    run.add_argument("--env-file", action="append")
    run.add_argument("--model")
    run.add_argument("--reasoning")
    run.add_argument("--xctestrun")
    run.add_argument("--python", help="the interpreter for the arms (default: this one)")
    run.add_argument("--timeout", type=float, default=TASK_TIMEOUT)
    run.add_argument("--ttft", default="on", help="MOBSTER_BENCH_TTFT for the arms (on|off)")
    run.add_argument("--arm-env", action="append", help="ARM:KEY=VALUE, an arm's own environment (repeatable)")
    run.add_argument("--boot", action="store_true")
    run.add_argument("--shutdown", action="store_true")
    run.add_argument("--ledger")
    reap = sub.add_parser("reap", help=argparse.SUPPRESS)
    reap.add_argument("--parent", type=int, required=True)
    reap.add_argument("--active", required=True)
    reap.add_argument("--udids", default="")
    reap.add_argument("--shutdown", action="store_true")
    judge = sub.add_parser("judge", help="iOSWorld's judge on a batch, then the ledger")
    judge.add_argument("--batch", required=True)
    judge.add_argument("--repo", required=True)
    judge.add_argument("--env-file", action="append")
    judge.add_argument("--splits")
    judge.add_argument("--ledger")
    judge.add_argument("--workers", type=int, default=4)
    trace = sub.add_parser("trace", help="write trace.jsonl in every task folder of a batch")
    trace.add_argument("--batch", required=True)
    board = sub.add_parser("scoreboard", help="the batch's scoreboard (Markdown)")
    board.add_argument("--batch", required=True)
    board.add_argument("--labels")
    board.add_argument("--ledger")
    board.add_argument("--out")
    label = sub.add_parser("label", help="label each failed run's root cause with a model")
    label.add_argument("--batch", required=True)
    label.add_argument("--model", default="claude-sonnet-5-5")
    label.add_argument("--reasoning", default="low")
    label.add_argument("--env-file", action="append")
    label.add_argument("--out", help="labels JSONL (default: BATCH/labels.jsonl)")
    cf = sub.add_parser("counterfactual", help="re-ask recorded turns under a variant (offline)")
    cf.add_argument("--runs", nargs="+", required=True)
    cf.add_argument("--variant", default="mobile_agent.bench.tuning_analysis:identity")
    cf.add_argument("--model", default="claude-sonnet-5-5")
    cf.add_argument("--reasoning", default="low")
    cf.add_argument("--n", type=int, default=40)
    cf.add_argument("--seed", type=int, default=20261010)
    cf.add_argument("--outcome", choices=("passed", "failed"))
    cf.add_argument("--env-file", action="append")
    cf.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return {"run": cmd_run, "reap": cmd_reap, "judge": cmd_judge, "trace": cmd_trace, "scoreboard": cmd_scoreboard,
            "label": cmd_label, "counterfactual": cmd_counterfactual}[args.command](args) or 0


def _load_envs(paths):
    from .iosworld import load_envs
    load_envs(paths)


def _client(model, reasoning):
    from ..frontier import chat_client
    from .stream_timing import timed
    return timed(chat_client(model, reasoning=reasoning))


def cmd_trace(args):
    from .tuning_analysis import task_folders, write_trace
    folders = task_folders(args.batch)
    for folder in folders:
        write_trace(folder)
    print(f"{len(folders)} traces written")


def cmd_scoreboard(args):
    from .tuning_analysis import scoreboard
    text = "\n".join(scoreboard(args.batch, args.labels, args.ledger)) + "\n"
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(text)


def cmd_label(args):
    from .tuning_analysis import label_failures
    _load_envs(args.env_file)
    out = args.out or str(pathlib.Path(args.batch) / "labels.jsonl")
    spent = label_failures(args.batch, _client(args.model, args.reasoning), out)
    print(f"labeller spend: ${spent:.4f} -> {out}")


def cmd_counterfactual(args):
    from .tuning_analysis import counterfactual, load_variant, recorded_turns
    _load_envs(args.env_file)
    outcome = {"passed": True, "failed": False}.get(args.outcome)
    turns = recorded_turns(args.runs, args.n, seed=args.seed, outcome=outcome)
    rows, summary = counterfactual(turns, _client(args.model, args.reasoning), load_variant(args.variant))
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = args.variant.rsplit(":", 1)[-1]
    (out / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = {"variant": args.variant, "model": args.model, **summary}
    with open(out / "summary.jsonl", "a") as handle:
        handle.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    sys.exit(main())

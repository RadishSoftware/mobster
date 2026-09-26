"""Runs the plan: health check, reset, agent, grade, record. Resumable, order-randomized.

Hygiene, in order, for every attempt:
1. ``phone.health``: WDA answers, has a session, the phone is NOT locked. A
   locked or unresponsive phone halts the run (resume later with ``--resume``).
2. Yield (GET only) while the installed Mobster app has a queued/running task.
3. Reset under the exclusive device lease: terminate + relaunch the start app,
   pop to its root (or open the start URL), verified by the probe. Start-screen
   values that change day to day are captured here (``truth.capture_from_reset``).
4. The agent runs against the task's time and step budgets.
5. Grading: the probe (own WDA connection) reads the device; checks compare
   with ground truth fixed before the run.
6. One JSON line per attempt, flushed and fsynced. ``--resume`` skips attempts
   already recorded (infrastructure failures are retried).

Order: for each repeat, tasks are shuffled and, per task, agents are shuffled,
from a seed stored in the plan, so time-of-day drift and warm caches spread
across agents instead of favouring whichever ran first.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
import os
from pathlib import Path
import random
import subprocess
import time

from .agents.base import AgentRun, Context
from .checks import CheckContext, grade
from .phone import health as phone_health
from .safety import Monitor

INFRA = "infra"          # reset/health failure: not the agent's fault, retried on resume
SKIPPED = "skipped"      # agent unavailable or fixture missing: counted, never graded
TIMEOUT_GRACE_S = 2.0


class RunHalted(Exception):
    """The phone is locked or unresponsive: stop now, resume later."""


@dataclass(frozen=True)
class Attempt:
    repeat: int
    order: int
    task: str
    agent: str

    @property
    def key(self):
        return f"{self.repeat}:{self.task}:{self.agent}"


def make_plan(task_ids, agent_names, repeats, seed):
    plan, order = [], 0
    for repeat in range(repeats):
        tasks = list(task_ids)
        random.Random(f"{seed}:{repeat}").shuffle(tasks)
        for task in tasks:
            agents = list(agent_names)
            random.Random(f"{seed}:{repeat}:{task}").shuffle(agents)
            for agent in agents:
                plan.append(Attempt(repeat, order, task, agent))
                order += 1
    return plan


def git_state(root):
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "mobile_agent"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
        return {"commit": commit, "mobile_agent_dirty": dirty}
    except Exception:
        return {"commit": None}


# Seconds the harness waits out a busy WDA before calling an attempt infrastructure-failed.
HEALTH_PATIENCE = 20
HEALTH_POLL = 2
RESET_TRIALS = 2
RESET_RETRY_PAUSE = 3


def app_idle(url, wait_seconds=600, sleep=time.sleep, poll=5):
    """GET-only: True once the installed Mobster app has no queued/running task (or is not running)."""
    import urllib.error
    import urllib.request
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/runs", timeout=3) as response:
                runs = json.loads(response.read().decode()).get("runs", [])
        except urllib.error.URLError as error:
            if isinstance(getattr(error, "reason", None), ConnectionRefusedError):
                return True  # the app is not running: nothing to yield to
            runs = None
        except (OSError, ValueError, AttributeError):
            runs = None
        if runs is not None and not any(isinstance(r, dict) and r.get("status") in ("queued", "running")
                                        for r in runs):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(poll)


def load_records(path):
    records = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue  # a torn last line from an interruption
    return records


class Runner:
    def __init__(self, *, tasks, agents, out_dir, wda_url, device, truth, fixtures, repeats=3, seed=None,
                 yield_to="http://127.0.0.1:8765", probe_factory=None, phone_factory=None, health=phone_health,
                 fixture_verifier_factory=None, lease_factory=None, log=print, clock=time.monotonic,
                 halt_after_failures=2, sleep=time.sleep):
        self.tasks = {task.id: task for task in tasks}
        self.agents = agents
        self.out = Path(out_dir)
        self.wda_url = wda_url
        self.device = device
        self.truth = truth
        self.fixtures = fixtures
        self.repeats = repeats
        self.seed = seed if seed is not None else int(time.time())
        self.yield_to = yield_to
        self.probe_factory = probe_factory
        self.phone_factory = phone_factory
        self.health = health
        self.fixture_verifier_factory = fixture_verifier_factory
        self.lease_factory = lease_factory
        self.log = log
        self.clock = clock
        self.halt_after_failures = halt_after_failures
        self.sleep = sleep
        self.availability = {}
        self.fixture_status = {}

    # ------------------------------------------------------------------ plan
    @property
    def plan_path(self):
        return self.out / "plan.json"

    @property
    def records_path(self):
        return self.out / "records.jsonl"

    def prepare(self, suite_hash, header_extra=None):
        """Create or load the plan. A resumed run keeps its original seed, order and suite hash."""
        self.out.mkdir(parents=True, exist_ok=True)
        if self.plan_path.exists():
            header = json.loads(self.plan_path.read_text())
            if header["suite_hash"] != suite_hash:
                raise SystemExit("The suite changed since this run started; start a new run directory.")
            self.seed = header["seed"]
            plan = [Attempt(**item) for item in header["attempts"]]
            return header, plan
        plan = make_plan(list(self.tasks), list(self.agents), self.repeats, self.seed)
        header = {"run_id": self.out.name, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "suite_hash": suite_hash, "seed": self.seed, "repeats": self.repeats, "device": self.device,
                  "wda_url": self.wda_url, "truth_digest": self.truth.digest(),
                  "agents": {name: {"config": _safe_config(agent)} for name, agent in self.agents.items()},
                  "tasks": sorted(self.tasks), **(header_extra or {}),
                  "attempts": [asdict(attempt) for attempt in plan]}
        self.plan_path.write_text(json.dumps(header, indent=1))
        return header, plan

    # ------------------------------------------------------------------ run
    def run(self, suite_hash, header_extra=None, max_attempts=None):
        header, plan = self.prepare(suite_hash, header_extra)
        done = {r["key"] for r in load_records(self.records_path) if r.get("verdict") != INFRA}
        pending = [attempt for attempt in plan if attempt.key not in done]
        self.log(f"{len(plan)} attempts planned, {len(plan) - len(pending)} already recorded, "
                 f"{len(pending)} to go (seed {self.seed})")
        for name, agent in self.agents.items():
            if name not in self.availability:
                try:
                    self.availability[name] = agent.available()
                except Exception as error:
                    self.availability[name] = (False, f"{type(error).__name__}: {error}")
                self.log(f"  agent {name}: {'available' if self.availability[name][0] else 'SKIPPED'}"
                         f" ({self.availability[name][1]})")
        failures, count = 0, 0
        for attempt in pending:
            if max_attempts is not None and count >= max_attempts:
                break
            record = self.attempt(attempt)
            self._append(record)
            count += 1
            self.log(_line(record))
            if record["verdict"] == INFRA:
                failures += 1
                if failures >= self.halt_after_failures:
                    raise RunHalted(f"{failures} consecutive infrastructure failures; last: {record.get('error')}")
            else:
                failures = 0
        return self.records_path

    def _patient_health(self):
        """Health, re-checked for a while when WDA is only busy; a locked phone answers at once."""
        deadline = None
        while True:
            health = self.health(self.wda_url)
            if health.ok or health.locked:
                return health
            deadline = self.clock() + HEALTH_PATIENCE if deadline is None else deadline
            if self.clock() >= deadline:
                return health
            self.sleep(HEALTH_POLL)

    def attempt(self, attempt):
        task = self.tasks[attempt.task]
        agent = self.agents[attempt.agent]
        base = {"key": attempt.key, "repeat": attempt.repeat, "order": attempt.order, "task": task.id,
                "category": task.category, "agent": attempt.agent,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        available, reason = self.availability.get(attempt.agent, (True, ""))
        if not available:
            return {**base, "verdict": SKIPPED, "error": f"agent unavailable: {reason}"}
        missing = [key for key in task.fixtures if self.fixture_status.get(key, (None,))[0] is False]
        if missing:
            return {**base, "verdict": SKIPPED, "error": f"fixture missing on phone: {missing}"}

        health = self._patient_health()
        if not health.ok:
            if health.locked:
                raise RunHalted("phone is locked; unlock it and resume")
            return {**base, "verdict": INFRA, "error": f"health: {health.reason}"}
        if self.yield_to and not app_idle(self.yield_to, sleep=self.sleep):
            return {**base, "verdict": INFRA, "error": "the Mobster app kept the phone busy"}

        # Reset (harness-owned, under the device lease), then capture start-screen truth.
        # One retry after a pause: WDA serves one request at a time, and a read the
        # previous attempt abandoned can hold it for seconds (pass 1, 24 Sep: 6 of 8
        # infra verdicts were this, with the runner never restarting).
        reset_started = self.clock()
        captured = {}
        for trial in range(RESET_TRIALS):
            try:
                with self._lease():
                    probe = self.probe_factory(self.wda_url)
                    try:
                        start = probe.reset(task.bundle, task.start_url or None)
                    finally:
                        _close(probe)
                    from .truth import capture_from_reset
                    captured = capture_from_reset(start, task.bundle)
                break
            except Exception as error:
                if trial + 1 == RESET_TRIALS:
                    return {**base, "verdict": INFRA, "error": f"reset failed: {type(error).__name__}: {error}"}
                self.sleep(RESET_RETRY_PAUSE)
        reset_ms = (self.clock() - reset_started) * 1000
        self.sleep(0.5)  # let the relaunched app finish its first layout pass

        monitor = Monitor(task)
        phone = None
        try:
            if getattr(agent, "kind", "") == "baseline":
                phone = self.phone_factory(self.wda_url, health.session)
        except Exception as error:
            return {**base, "verdict": INFRA, "error": f"phone setup failed: {type(error).__name__}: {error}"}
        started = self.clock()
        ctx = Context(phone=phone, wda_url=self.wda_url, monitor=monitor, deadline=started + task.max_seconds,
                      started=started, session=health.session or "",
                      artifacts=str(self.out / "artifacts" / re.sub(r"[^\w.-]", "_", attempt.key)))
        try:
            if getattr(agent, "kind", "") == "baseline":
                with self._lease():
                    run = agent.run(task, ctx)
            else:
                run = agent.run(task, ctx)  # Mobster takes the device lease itself, as serve does.
        except Exception as error:
            run = AgentRun(status="error", reason=f"{type(error).__name__}: {error}")
        finally:
            if phone is not None:
                try:
                    phone.close()
                except Exception:
                    pass
        wall_ms = (self.clock() - started) * 1000
        run.wall_ms = round(wall_ms, 1)
        run.monitor_ms = round(monitor.monitor_ms, 1)
        throttle_ms = (run.detail or {}).get("throttle_ms", 0)
        run.agent_ms = round(wall_ms - monitor.monitor_ms - throttle_ms, 1)
        run.unsafe = monitor.unsafe
        run.unintended = monitor.unintended
        if run.unsafe and run.status not in ("unsafe_stopped",):
            run.status = "unsafe_stopped"
        if run.agent_ms > (task.max_seconds + TIMEOUT_GRACE_S) * 1000 and run.status != "unsafe_stopped":
            # The budget is hard for every agent: a late answer is a timeout.
            run.detail["late_status"] = run.status
            run.status, run.answer, run.abstained = "timeout", None, False

        grade_started = self.clock()
        record_run = run.to_dict()
        try:
            with self._lease():
                probe = self.probe_factory(self.wda_url)
                verifier = self.fixture_verifier_factory(probe) if self.fixture_verifier_factory else None
                ctx_checks = CheckContext(probe=probe, truth=_TruthView(self.truth), captured=captured,
                                          fixtures=self.fixtures, fixture_verifier=verifier)
                try:
                    verdict, checks = grade(task.checks, record_run, ctx_checks)
                finally:
                    _close(probe)
        except Exception as error:
            verdict, checks = "ungraded", [{"check": "grading", "ok": None,
                                            "detail": f"{type(error).__name__}: {error}"}]
        grade_ms = (self.clock() - grade_started) * 1000
        return {**base, "verdict": verdict, "checks": checks, "captured": captured,
                "reset_ms": round(reset_ms, 1), "grade_ms": round(grade_ms, 1), **record_run}

    # ------------------------------------------------------------------ helpers
    def _lease(self):
        if self.lease_factory is None:
            from ..journal import Lease

            class _Held:
                def __init__(inner, url):
                    inner.url = url

                def __enter__(inner):
                    inner.lease = Lease.device(inner.url)
                    return inner

                def __exit__(inner, *exc):
                    inner.lease.close()
                    return False
            return _Held(self.wda_url)
        return self.lease_factory(self.wda_url)

    def _append(self, record):
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.records_path, "a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class _TruthView:
    """Checks read truth through ``.get(key)``."""

    def __init__(self, store):
        self.store = store

    def get(self, key):
        return self.store.get(key)


def _close(thing):
    closer = getattr(thing, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _safe_config(agent):
    try:
        return agent.config()
    except Exception as error:
        return {"error": type(error).__name__}


def _line(record):
    verdict = record["verdict"].upper()
    if record["verdict"] in (INFRA, SKIPPED):
        return f"  [{record['repeat'] + 1}] {verdict:8} {record['task']:28} {record['agent']:13} {record.get('error', '')[:90]}"
    failing = "; ".join(f"{c['check']}: {c['detail']}" for c in record.get("checks", []) if c.get("ok") is False)
    return (f"  [{record['repeat'] + 1}] {verdict:8} {record['task']:28} {record['agent']:13} "
            f"{record.get('agent_ms', 0) / 1000:6.1f}s steps={record.get('decision_steps')} "
            f"calls={record.get('model_calls')} {failing[:110]}")

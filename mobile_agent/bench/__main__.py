"""MobsterBench-iOS command line.

  python -m mobile_agent.bench list                      # the frozen suite
  python -m mobile_agent.bench freeze                    # (pre-registration) write manifest.json
  python -m mobile_agent.bench fixtures --out DIR        # image folders + setup sheet for the phone
  python -m mobile_agent.bench capture-truth --wda-url http://127.0.0.1:8100
  python -m mobile_agent.bench dry-plan [--probe] [--probe-models]
  python -m mobile_agent.bench run --wda-url http://127.0.0.1:8100 --out research/bench-runs/NAME
  python -m mobile_agent.bench report --run research/bench-runs/NAME

Secrets: the env file is loaded into this process only; no key, token or
project id is ever printed.
"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from . import suite as suite_module
from ..paths import source_checkout
from .agents import AGENT_NAMES, DEFAULT_AGENTS, build_agents
from .fixtures import Fixtures
from .truth import TruthStore

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
REPO = HERE.parent.parent
# Default output root: the checkout, or where an installed copy is run from (never site-packages).
RUNS = REPO if source_checkout() else Path.cwd()
DEFAULT_ENV = Path.home() / "Library" / "Application Support" / "app.mobster.desktop" / "agent.env"

# Planning priors (estimates, not benchmark results): agent seconds per attempt, decision steps per
# attempt, and $ per decision step. Mobster: latency breakdown, 23 Sep 2026. Baselines: one
# wire-format check on 2026-09-23 against Vertex with a synthetic Settings screen (n=2 calls each):
# computer use on gemini-3.7-flash took ~29 s per call (gemini-3.8-flash was returning HTTP 429), and
# set-of-marks on gemini-3.1-pro-preview ~4 s per call; plus screenshot, AX read and the 1 s settle.
# Every estimate is capped by the task's time budget.
PRIOR_STEPS = {"navigation": 4, "retrieval": 5, "settings_state": 4, "text_entry": 6, "multi_app": 12,
               "web": 4, "scroll": 7, "visual": 30}
PRIOR_MOBSTER_S = {"navigation": 9, "retrieval": 10, "settings_state": 9, "text_entry": 14, "multi_app": 30,
                   "web": 10, "scroll": 16, "visual": 60}
PRIOR_STEP_S = {"gemini-cu": 31.0, "som-pro": 6.5, "som-flash": 5.0}
PRIOR_STEP_USD = {"gemini-cu": 0.008, "som-pro": 0.012, "som-flash": 0.007}
PRIOR_MOBSTER_USD = 0.003
OVERHEAD_S = 7.0   # health + reset + grading per attempt


def load_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None


def cmd_list(args):
    tasks = suite_module.build_suite()
    manifest = load_manifest()
    live = suite_module.suite_hash(tasks)
    print(f"{suite_module.SUITE_NAME} v{suite_module.SUITE_VERSION}: {len(tasks)} tasks")
    print(f"hash {live}  ({'matches' if manifest and manifest['sha256'] == live else 'DOES NOT match'} manifest)")
    for category, count in suite_module.category_counts(tasks).items():
        print(f"  {category:15} {count}")
    if args.verbose:
        for task in tasks:
            print(f"{task.id:30} {task.category:14} {task.safety:18} {task.max_steps:3} steps "
                  f"{task.max_seconds:4.0f}s  {task.goal[:70]}")


def cmd_freeze(args):
    tasks = suite_module.build_suite()
    digest = suite_module.suite_hash(tasks)
    manifest = load_manifest()
    if manifest and manifest["sha256"] != digest and not args.force:
        raise SystemExit("manifest.json holds a different frozen hash. A frozen suite is not edited: bump "
                         "SUITE_VERSION and pass --force to register a new version.")
    body = {"suite": suite_module.SUITE_NAME, "version": suite_module.SUITE_VERSION, "sha256": digest,
            "frozen_on": (manifest or {}).get("frozen_on") if manifest and manifest["sha256"] == digest
            else date.today().isoformat(),
            "task_count": len(tasks), "categories": suite_module.category_counts(tasks),
            "safety_classes": {cls: sum(t.safety == cls for t in tasks) for cls in suite_module.SAFETY_CLASSES},
            "tasks": [t.id for t in tasks],
            "note": "Written before any benchmark run. The runner refuses a suite whose hash differs."}
    MANIFEST.write_text(json.dumps(body, indent=1) + "\n")
    print(f"frozen {len(tasks)} tasks: {digest}")


def cmd_fixtures(args):
    out = Fixtures().build(args.out)
    print(f"wrote {out}/copy-to-phone and {out}/SETUP.txt (labels kept outside the phone folder)")


def _probe(url):
    from ..evals.oracles import WDAProbe
    return WDAProbe(url)


def cmd_capture_truth(args):
    from .phone import health
    from .truth import capture_truth
    state = health(args.wda_url)
    if not state.ok:
        raise SystemExit(f"phone not ready: {state.reason}")
    store = TruthStore(args.device)
    capture_truth(_probe(args.wda_url), store)
    store.save()
    print(f"saved {store.path}")


def estimate(tasks, agent_names, repeats):
    rows = {}
    for name in agent_names:
        seconds = usd = 0.0
        for task in tasks:
            if name.startswith("mobster"):
                seconds += min(PRIOR_MOBSTER_S[task.category], task.max_seconds)
                usd += PRIOR_MOBSTER_USD
            else:
                steps = min(PRIOR_STEPS[task.category], task.max_steps,
                            max(1, int(task.max_seconds // PRIOR_STEP_S[name])))
                seconds += min(steps * PRIOR_STEP_S[name], task.max_seconds)
                usd += steps * PRIOR_STEP_USD[name]
        rows[name] = (repeats * (seconds + OVERHEAD_S * len(tasks)), repeats * usd)
    return rows


def cmd_dry_plan(args):
    """Validate suite, truth, fixtures and agents without letting any agent act."""
    tasks = _select(args)
    manifest = load_manifest()
    digest = suite_module.suite_hash()
    problems = []
    print(f"suite hash {digest[:16]}… "
          f"{'== manifest' if manifest and manifest['sha256'] == digest else '!= manifest (NOT FROZEN)'}")
    if not manifest or manifest["sha256"] != digest:
        problems.append("suite not frozen / hash mismatch")
    store = TruthStore(args.device)
    needed = sorted({c.expect.key for t in tasks for c in t.checks
                     if hasattr(c, "expect") and type(c.expect).__name__ == "Truth"})
    missing = store.missing(needed)
    unconfirmed = [k for k in store.unconfirmed() if k in needed]
    print(f"truth: {len(needed)} keys needed, {len(missing)} missing, {len(unconfirmed)} assumed (unconfirmed)")
    for key in missing:
        print(f"  MISSING   {key}  -> run capture-truth (tasks using it are ungraded until then)")
    for key in unconfirmed:
        print(f"  ASSUMED   {key} = {store.get(key)!r}")
    fixtures_needed = sorted({f for t in tasks for f in t.fixtures})
    print(f"fixtures: {len(fixtures_needed)} needed by {sum(bool(t.fixtures) for t in tasks)} tasks: "
          f"{', '.join(fixtures_needed)}")
    for task in tasks:
        try:
            for check in task.checks:
                ref = getattr(check, "expect", None)
                if type(ref).__name__ == "Fixture":
                    Fixtures().value(ref.key, ref.part)
        except Exception as error:
            problems.append(f"{task.id}: {error}")
    names = [n for n in args.agents.split(",") if n]
    agents = build_agents(names, env_file=args.env_file)
    for name, agent in agents.items():
        if name.startswith("mobster") or args.probe_models:
            ok, reason = agent.available()
            print(f"agent {name:13} {'available' if ok else 'UNAVAILABLE'}: {reason}")
        else:
            print(f"agent {name:13} not probed (pass --probe-models to make one tiny Vertex call per model)")
    print(f"\nplan: {len(tasks)} tasks x {len(names)} agents x {args.repeats} repeats = "
          f"{len(tasks) * len(names) * args.repeats} attempts")
    total_s = total_usd = 0.0
    for name, (seconds, usd) in estimate(tasks, names, args.repeats).items():
        total_s += seconds
        total_usd += usd
        print(f"  estimate {name:13} {seconds / 3600:5.1f} h  ${usd:6.2f}")
    print(f"  estimate TOTAL         {total_s / 3600:5.1f} h  ${total_usd:6.2f}   (priors, not measurements)")
    if args.probe:
        _probe_device(args, tasks, problems)
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems))
        return 1
    print("\ndry plan OK")
    return 0


def _probe_device(args, tasks, problems):
    """Read-only device checks: health, lock, and each distinct start state resets and reads."""
    from ..evals.oracles import ProbeUnavailable
    from .phone import health
    from .truth import capture_from_reset, fixture_verifier
    state = health(args.wda_url)
    print(f"\ndevice: {'ready' if state.ok else 'NOT READY: ' + state.reason}")
    if not state.ok:
        problems.append(f"device: {state.reason}")
        return
    probe = _probe(args.wda_url)
    for bundle, url in sorted({(t.bundle, t.start_url) for t in tasks}):
        try:
            start = probe.reset(bundle, url or None)
            extra = capture_from_reset(start, bundle)
            print(f"  reset OK   {bundle} {url or ''} {extra if extra else ''}")
        except ProbeUnavailable as error:
            problems.append(f"reset {bundle}: {error}")
            print(f"  reset FAIL {bundle}: {error}")
    if args.verify_fixtures:
        verify = fixture_verifier(probe, Fixtures())
        for key in sorted({f for t in tasks for f in t.fixtures}):
            ok, detail = verify(key)
            print(f"  fixture {key:22} {detail}")
            if ok is False:
                problems.append(f"fixture {key}: {detail}")


def _select(args):
    tasks = suite_module.build_suite()
    only = [part for part in (getattr(args, "only", None) or "").split(",") if part]
    if only:
        tasks = tuple(t for t in tasks if any(part == t.id or part == t.category for part in only))
    return tasks


def cmd_run(args):
    from ..evals.oracles import WDAProbe
    from .phone import Phone
    from .runner import RunHalted, Runner, git_state
    from .truth import fixture_verifier
    manifest = load_manifest()
    digest = suite_module.suite_hash()
    if not manifest or manifest["sha256"] != digest:
        if not args.allow_unfrozen:
            raise SystemExit("suite hash != manifest.json: freeze first (or --allow-unfrozen for development).")
    tasks = _select(args)
    if not tasks:
        raise SystemExit("no tasks selected")
    names = [n for n in args.agents.split(",") if n]
    cu_model, som_model = _resolve_models(args, names)
    agents = build_agents(names, env_file=args.env_file, cu_model=cu_model, som_model=som_model,
                          settle=args.settle)
    fixtures = Fixtures()
    out = Path(args.resume or args.out or RUNS / "research" / "bench-runs" / f"run-{date.today().isoformat()}")
    runner = Runner(tasks=tasks, agents=agents, out_dir=out, wda_url=args.wda_url, device=args.device,
                    truth=TruthStore(args.device), fixtures=fixtures, repeats=args.repeats, seed=args.seed,
                    yield_to=None if args.no_yield else args.yield_to, probe_factory=WDAProbe,
                    phone_factory=lambda url, session: Phone(url, session),
                    fixture_verifier_factory=lambda probe: fixture_verifier(probe, fixtures))
    if args.verify_fixtures:
        verify = fixture_verifier(WDAProbe(args.wda_url), fixtures)
        for key in sorted({f for t in tasks for f in t.fixtures}):
            runner.fixture_status[key] = verify(key)
            print(f"fixture {key}: {runner.fixture_status[key][1]}")
    try:
        runner.run(digest, header_extra={"git": git_state(REPO), "manifest_frozen": bool(
            manifest and manifest["sha256"] == digest)}, max_attempts=args.max_attempts)
    except RunHalted as halt:
        print(f"\nHALTED: {halt}\nResume with: python -m mobile_agent.bench run --resume {out} "
              f"--wda-url {args.wda_url}")
        return 2
    print(f"\nrecords: {runner.records_path}")
    return 0


def _resolve_models(args, names):
    """'auto' picks the best model that answers now; a resumed run keeps the models it started with."""
    from .vertex import CU_PREFERENCE, SOM_PREFERENCE, Vertex, pick_model
    cu_model, som_model = args.cu_model, args.som_model
    plan = Path(args.resume) / "plan.json" if args.resume else None
    if plan and plan.exists():
        agents = json.loads(plan.read_text()).get("agents", {})
        cu_model = (agents.get("gemini-cu", {}).get("config") or {}).get("model", cu_model)
        som_model = (agents.get("som-pro", {}).get("config") or {}).get("model", som_model)
    if "auto" in (cu_model, som_model) and any(n in names for n in ("gemini-cu", "som-pro")):
        vertex = Vertex()
        if cu_model == "auto" and "gemini-cu" in names:
            print("choosing the computer-use model:")
            cu_model = pick_model(vertex, CU_PREFERENCE, print) or CU_PREFERENCE[0]
        if som_model == "auto" and "som-pro" in names:
            print("choosing the set-of-marks model:")
            som_model = pick_model(vertex, SOM_PREFERENCE, print) or SOM_PREFERENCE[0]
    return (CU_PREFERENCE_DEFAULT if cu_model == "auto" else cu_model,
            SOM_PREFERENCE_DEFAULT if som_model == "auto" else som_model)


CU_PREFERENCE_DEFAULT, SOM_PREFERENCE_DEFAULT = "gemini-3.8-flash", "gemini-3.1-pro-preview"


def cmd_report(args):
    from .report import render
    out = args.out or RUNS / "research" / f"bench-{date.today().isoformat()}.md"
    render(args.run, out)
    print(f"wrote {out}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("freeze")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("fixtures")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_fixtures)

    def device_args(p):
        p.add_argument("--wda-url", default="http://127.0.0.1:8100")
        p.add_argument("--device", default="iphone15pro")
        p.add_argument("--env-file", default=str(DEFAULT_ENV))

    p = sub.add_parser("capture-truth")
    device_args(p)
    p.set_defaults(func=cmd_capture_truth)

    p = sub.add_parser("dry-plan")
    device_args(p)
    p.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--only")
    p.add_argument("--probe", action="store_true", help="read-only device checks (resets start apps)")
    p.add_argument("--verify-fixtures", action="store_true", help="with --probe: read-only fixture checks")
    p.add_argument("--probe-models", action="store_true", help="one tiny Vertex call per baseline model")
    p.set_defaults(func=cmd_dry_plan)

    p = sub.add_parser("run")
    device_args(p)
    p.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help=f"comma list of {AGENT_NAMES}")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--seed", type=int)
    p.add_argument("--only", help="comma list of task ids or categories")
    p.add_argument("--out")
    p.add_argument("--resume", help="an existing run directory")
    p.add_argument("--max-attempts", type=int, help="stop after this many attempts (smoke tests)")
    p.add_argument("--cu-model", default="auto", help="auto = best of vertex.CU_PREFERENCE that answers now")
    p.add_argument("--som-model", default="auto", help="auto = best of vertex.SOM_PREFERENCE that answers now")
    p.add_argument("--settle", type=float, default=1.0, help="baselines' fixed post-action settle (s)")
    p.add_argument("--yield-to", default="http://127.0.0.1:8765", help="GET-only: wait while the app is busy")
    p.add_argument("--no-yield", action="store_true")
    p.add_argument("--verify-fixtures", action="store_true")
    p.add_argument("--allow-unfrozen", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report")
    p.add_argument("--run", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if getattr(args, "env_file", None):
        from ..config import load_env_file
        load_env_file(args.env_file)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

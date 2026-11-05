#!/usr/bin/env python3
"""Runs the backend test suite in parallel, one process per test module.

`make test-backend` and local CI use this runner. Each module has its own process,
so tests cannot leak registrations or environment changes into another module. The slowest
modules start first, ordered by the durations the previous run recorded in .ci-cache/.
A module that fails is run once more on its own; one that then passes is reported as flaky,
with its first failure, and does not fail the run.

Each module gets its own home and temporary folders. The device lease lives in the user's data
folder and its legacy twin in $TMPDIR, so concurrent demo runs would otherwise queue on one
lease; the tests also never touch the real Mac app's data folder.

With MOBSTER_CI_CACHE set (local CI does), a module that passed before is not run again when nothing it can reach has
changed: its key is a hash of its test file, every Python file in its import closure, every file a module in the
closure names, the files no test names, the Python version, the requirements and this runner. A module whose reach
cannot be known (a computed import, a file that does not parse), the safety core and any failure are never cached.
--no-result-cache runs everything (and still stores the passes).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # --root replaces it: ci-local.sh checks other commits
# ci-local.sh points this at the cache every worktree shares; a bare run keeps it in the checkout.
TIMINGS = Path(os.environ.get("MOBSTER_CI_CACHE") or ROOT / ".ci-cache") / "backend-timings.json"
# Passing results by key. Only a run under local CI (which sets MOBSTER_CI_CACHE) uses them; a bare run is always real.
RESULTS = Path(os.environ["MOBSTER_CI_CACHE"]) / "backend-results" if os.environ.get("MOBSTER_CI_CACHE") else None
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
# Seconds a test module may run. The slowest take about 20 s; a module past this is hung, not slow, and is stopped
# (then retried once alone, as a failure is) rather than holding the whole run for as long as nobody notices.
MODULE_LIMIT = 300
# This Mac is shared by many agents (AGENTS.md, "The shared Mac"): a run uses at most MAX_JOBS test processes, at a
# lower priority, and a whole-suite run waits while MAX_SUITES others are going.
MAX_JOBS = 4
MAX_SUITES = 2
# `python -m unittest MODULE`, which also prints every thread's stack on SIGUSR1: the log of a module stopped for
# its time shows where it waited.
UNITTEST = ("import faulthandler, signal, sys, unittest; faulthandler.register(signal.SIGUSR1); "
            "sys.argv[0] = 'python -m unittest'; unittest.main(module=None)")


def modules(root, start="mobile_agent"):
    """The modules `unittest discover -s mobile_agent -t .` finds: test*.py in packages."""
    found = []

    def walk(directory):
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                if (path / "__init__.py").is_file():
                    walk(path)
            elif path.name.startswith("test") and path.suffix == ".py":
                found.append(".".join(path.relative_to(root).with_suffix("").parts))

    walk(root / start)
    return found


def other_suites():
    """How many other whole-suite runs of this script are going on this Mac: Python processes running it without
    --modules, not counting this one, its ancestors (the `sh -c` make starts) or shells that only mention its name."""
    try:
        table = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return count_suites(table, os.getpid())


def count_suites(table, me):
    """The suites in `ps -axo pid=,ppid=,args=` output that aren't process `me` or one of its ancestors."""
    parents, suites = {}, []
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, args = int(parts[0]), parts[2]
        parents[pid] = int(parts[1])
        # A whole-suite run only: a gate's targeted run (--modules) is small and doesn't count toward the limit.
        if ("python" in os.path.basename(args.split()[0]).lower() and "ci_backend.py" in args
                and "--modules" not in args):
            suites.append(pid)
    mine, pid = set(), me
    while pid and pid not in mine:
        mine.add(pid)
        pid = parents.get(pid, 0)
    return sum(1 for pid in suites if pid not in mine)


def wait_for_room():
    """Waits until fewer than MAX_SUITES other suites run, saying so once."""
    said = False
    while other_suites() >= MAX_SUITES:
        if not said:
            print(f"Waiting: {MAX_SUITES} other backend suites are running on this Mac.", flush=True)
            said = True
        time.sleep(15)


def load_timings():
    try:
        return json.loads(TIMINGS.read_text())
    except (OSError, ValueError):
        return {}


def time_scale(env, load=None, cpus=None):
    """MOBSTER_TEST_TIME_SCALE for a module starting now (mobile_agent/tests/timing.py), or None for the strict
    bounds. A scale set by the caller always applies. Otherwise MOBSTER_CI_BUSY_SCALE (local CI sets it) applies
    when the 1-minute load is above the core count as the module starts: the load changes during a run, so it is
    read per module, not once for the whole run."""
    if env.get("MOBSTER_TEST_TIME_SCALE") or not env.get("MOBSTER_CI_BUSY_SCALE"):
        return None
    load = os.getloadavg()[0] if load is None else load
    cpus = (os.cpu_count() or 1) if cpus is None else cpus
    return env["MOBSTER_CI_BUSY_SCALE"] if load > cpus else None


def hashed(parts):
    digest = hashlib.sha256()
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode()
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def file_hash(root, path):
    try:
        return hashed([path, (Path(root) / path).read_bytes()])
    except OSError:
        return None


def module_keys(python, root, names):
    """{module: key} for the modules whose reach is known. A module missing from it is never cached. Any failure
    to compute the reach leaves it empty."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import ci_inputs
        import ci_select  # not in the public tree: without it nothing is cached
        graph = ci_select.Graph(root, RESULTS.parent / "select-ast.json")  # the memo ci-local.sh keeps
        reach, shared = graph.reach(ci_inputs.Rules(ci_inputs.MANIFEST.read_text()))
        version = subprocess.run([python, "-c", "import sys; print(sys.version)"], check=True, capture_output=True,
                                 text=True).stdout
    except (ImportError, OSError, ValueError, subprocess.SubprocessError):
        return {}
    finally:
        sys.path.pop(0)
    # MOBSTER_TEST_TIME_SCALE and the CI knobs change how long a test may take, not whether it passes.
    knobs = sorted((k, v) for k, v in os.environ.items() if k.startswith("MOBSTER_") and k != "MOBSTER_TEST_TIME_SCALE"
                   and not k.startswith("MOBSTER_CI_"))
    runner = [file_hash(Path(__file__).parent, name) for name in ("ci_backend.py", "ci_select.py", "ci_inputs.py")]
    requirements = [p.relative_to(root).as_posix()
                    for p in sorted(Path(root, "mobile_agent").glob("requirements*.txt"))]
    common = [version, knobs, runner, *(file_hash(root, p) for p in requirements),
              *(file_hash(root, p) for p in shared)]
    keys = {}
    for name in names:
        paths = reach.get(name)
        if paths is None or name in ci_select.SAFETY_CORE:
            continue
        hashes = [file_hash(root, path) for path in paths]
        if None not in hashes and None not in common:
            keys[name] = hashed([name, *common, *hashes])
    return keys


def cached_pass(key):
    try:
        return json.loads((RESULTS / key).read_text())
    except (OSError, ValueError, TypeError):
        return None


def store_pass(key, result):
    RESULTS.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS / f"{key}.{os.getpid()}"
    tmp.write_text(json.dumps({"tests": result["tests"], "skipped": result["skipped"],
                               "date": time.strftime("%Y-%m-%d")}))
    tmp.replace(RESULTS / key)


def run(python, root, module, env, scratch, limit=MODULE_LIMIT):
    started = time.monotonic()
    home, tmp = Path(scratch, module, "home"), Path(scratch, module, "tmp")
    home.mkdir(parents=True)
    tmp.mkdir()
    scale = time_scale(env)
    # Its own process group, so a module that hangs is stopped with everything it started.
    proc = subprocess.Popen(
        [python, "-c", UNITTEST, module],
        cwd=root,
        env=dict(env, HOME=str(home), TMPDIR=str(tmp), MOBSTER_MEMORY_DIR=str(home / "memory"),
                 **({"MOBSTER_TEST_TIME_SCALE": scale} if scale else {})),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=limit)
        late = False
    except subprocess.TimeoutExpired:
        late = True
        # The module can exit between the timeout and these signals; its stacks and group are then already gone.
        with suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGUSR1)
        time.sleep(1)
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as error:  # a process outside the group still holds the pipe
            output = error.output.decode(errors="replace") if isinstance(error.output, bytes) else error.output or ""
            proc.stdout.close()
            proc.wait()
        output += f"\nSTOPPED: still running after {limit:.0f}s (the stacks above show where it waited)\n"
    ran = RAN.search(output)
    skipped = SKIPPED.search(output[-200:])  # the summary line: OK (skipped=1)
    return {
        "module": module,
        # A module that dies before unittest prints its summary (os._exit, a signal) shows only this.
        "returncode": proc.returncode,
        # A test*.py of shared fakes holds no tests: discovery skips it, a run by name exits 5.
        "ok": not late and (proc.returncode == 0 or (proc.returncode == 5 and "NO TESTS RAN" in output)),
        "tests": int(ran.group(1)) if ran else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "seconds": time.monotonic() - started,
        "output": output,
        "scaled": bool(scale),
    }


def behind_requirements(python, root):
    """["cryptography 50.0.1 (pinned 50.0.2)", ...]: the packages ``python`` has at another version than the lock
    (mobile_agent/requirements.txt). A venv made before a pin moved fails tests that read the pin, with a message about
    the test, not the venv; this names the venv. Empty when all match,
    or when the lock or the packages can't be read."""
    lock = root / "mobile_agent" / "requirements.txt"
    try:
        pins = {name.lower().replace("_", "-"): version for name, version in
                re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", lock.read_text(), re.M)}
        probe = subprocess.run(
            [python, "-I", "-c", "import importlib.metadata as m, json, sys; print(json.dumps({n: (lambda v: v)("
             "next((d.version for d in m.distributions() if d.metadata['Name'].lower().replace('_', '-') == n), None)) "
             "for n in json.loads(sys.argv[1])}))", json.dumps(sorted(pins))],
            capture_output=True, text=True, timeout=60, check=True)
        have = json.loads(probe.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [f"{name} {have[name]} (pinned {pins[name]})" for name in sorted(pins)
            if have.get(name) is not None and have[name] != pins[name]]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=MAX_JOBS, help=f"test processes at once, at most {MAX_JOBS}")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to test")
    parser.add_argument("--running", action="store_true", help="print how many other suites are running and exit")
    parser.add_argument("--no-result-cache", action="store_true",
                        help="run every module, even one that passed with the same inputs (ci-local.sh --full does)")
    parser.add_argument("--modules", type=Path, help="a file naming the test modules to run, one per line "
                        "(scripts/ci_select.py writes it for ci-local.sh --fast); the default is every module")
    args = parser.parse_args()
    if args.running:
        print(other_suites())
        return
    args.jobs = max(1, min(args.jobs, MAX_JOBS))
    behind = behind_requirements(args.python, args.root.resolve())
    if behind:
        print("This Python is behind mobile_agent/requirements.txt: " + ", ".join(behind) + ". Run `make venv`, then "
              "run the tests again.", file=sys.stderr)
        sys.exit(2)
    with suppress(OSError):
        os.nice(10)
    if not args.modules:
        wait_for_room()
    root = args.root.resolve()

    names = modules(root)
    if args.modules:
        wanted = set(args.modules.read_text().split())
        names = [name for name in names if name in wanted]
    timings = load_timings()
    # Unknown modules (new since the last run) go first: they may be slow.
    longest = max(timings.values(), default=0.0) + 1
    # A pre-merge run (--modules) starts the safety core first, so a red there shows within a minute.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from ci_select import SAFETY_CORE
    except ImportError:  # not in the public tree
        SAFETY_CORE = ()
    finally:
        sys.path.pop(0)
    names.sort(key=lambda name: (bool(args.modules) and name not in SAFETY_CORE, -timings.get(name, longest)))
    # MOBSTER_NO_DEVICES=1: wireless.devicectl, phone_io.files and mobster-wifi-pair raise instead of running
    # (native_helpers.devices_allowed), so no test reaches a phone even outside the sandbox.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", MOBSTER_NO_DEVICES="1")

    keys = {} if RESULTS is None else module_keys(args.python, root, names)
    # --no-result-cache still stores what passes, so the full run after a merge seeds the next gate.
    hits = {name: hit for name in names if name in keys and not args.no_result_cache
            and (hit := cached_pass(keys[name]))
            and isinstance(hit.get("tests"), int)}
    names = [name for name in names if name not in hits]

    scratch = os.path.realpath(tempfile.mkdtemp(prefix="mobster-ci-"))
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, args.python, root, name, env, scratch) for name in names]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = "ok" if result["ok"] else "failed (will retry)"
            print(f"backend [{len(results)}/{len(names)}] {result['module']}: {status} "
                  f"({result['seconds']:.1f}s)", flush=True)
            if not result["ok"]:
                print(result["output"], flush=True)
    flaky = []
    for index, result in enumerate(results):
        if result["ok"]:
            continue
        retry = run(args.python, root, result["module"], env, os.path.join(scratch, "retry"))
        if retry["ok"]:
            flaky.append(result["module"])
            print(f"FLAKY {result['module']} (returncode {result['returncode']}): failed in parallel, passed alone. "
                  f"The first run:\n{result['output']}")
            results[index] = dict(retry, seconds=result["seconds"])
        else:
            print(f"FAIL {result['module']} ({result['seconds']:.1f}s, returncode {result['returncode']}, and again "
                  "alone "
                  f"with returncode {retry['returncode']})\n{retry['output']}", flush=True)
    wall = time.monotonic() - started
    for result in results:
        # A module that failed or only passed on its retry is not stored.
        if result["ok"] and result["module"] in keys and result["module"] not in flaky:
            with suppress(OSError):
                store_pass(keys[result["module"]], result)
    shutil.rmtree(scratch, ignore_errors=True)

    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    # A run of some modules keeps the timings of the others.
    timings = timings if args.modules or hits else {}
    timings.update({r["module"]: round(r["seconds"], 2) for r in results})
    TIMINGS.write_text(json.dumps(timings, indent=1, sort_keys=True))

    failed = [r["module"] for r in results if not r["ok"]]
    tests = sum(r["tests"] for r in results) + sum(hit["tests"] for hit in hits.values())
    skipped = sum(r["skipped"] for r in results)
    busy = sum(r["seconds"] for r in results)
    slowest = sorted(results, key=lambda r: -r["seconds"])[:3]
    print(f"backend: {tests} tests in {len(results) + len(hits)} modules, {skipped} skipped, "
          f"{wall:.0f}s wall ({busy:.0f}s of work on {args.jobs} jobs)")
    if hits:
        print(f"cached {', '.join(sorted(hits))} ({len(hits)} modules, {sum(h['tests'] for h in hits.values())} tests "
              "that passed with the same inputs)")
    print("slowest: " + ", ".join(f"{r['module'].rsplit('.', 1)[-1]} {r['seconds']:.0f}s" for r in slowest))
    scaled = sum(r.get("scaled", False) for r in results)
    if scaled:
        print(f"busy: {scaled} of {len(results)} modules started with the load above {os.cpu_count()} cores, so their "
              f"timing bounds were scaled by {env['MOBSTER_CI_BUSY_SCALE']}")
    if flaky:
        print(f"flaky (passed on retry): {', '.join(flaky)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

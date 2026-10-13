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
"""

import argparse
import json
import os
import re
import subprocess
import shutil
import signal
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # --root replaces it: ci-local.sh checks other commits
# ci-local.sh points this at the cache every worktree shares; a bare run keeps it in the checkout.
TIMINGS = Path(os.environ.get("MOBSTER_CI_CACHE") or ROOT / ".ci-cache") / "backend-timings.json"
RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
# Seconds a test module may run. The slowest take about 20 s; a module past this is hung, not slow, and is stopped
# (then retried once alone, as a failure is) rather than holding the whole run for as long as nobody notices.
MODULE_LIMIT = 300
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


def run(python, root, module, env, scratch, limit=MODULE_LIMIT):
    started = time.monotonic()
    home, tmp = Path(scratch, module, "home"), Path(scratch, module, "tmp")
    home.mkdir(parents=True)
    tmp.mkdir()
    scale = time_scale(env)
    # Its own process group, so a module that hangs is stopped with everything it started.
    proc = subprocess.Popen(
        [python, "-c", UNITTEST, module],
        cwd=root, env=dict(env, HOME=str(home), TMPDIR=str(tmp), **({"MOBSTER_TEST_TIME_SCALE": scale} if scale else {})),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=limit)
        late = False
    except subprocess.TimeoutExpired:
        late = True
        os.kill(proc.pid, signal.SIGUSR1)
        time.sleep(1)
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
        # A test*.py of shared fakes holds no tests: discovery skips it, a run by name exits 5.
        "ok": not late and (proc.returncode == 0 or (proc.returncode == 5 and "NO TESTS RAN" in output)),
        "tests": int(ran.group(1)) if ran else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "seconds": time.monotonic() - started,
        "output": output,
        "scaled": bool(scale),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to test")
    args = parser.parse_args()
    root = args.root.resolve()

    names = modules(root)
    timings = load_timings()
    # Unknown modules (new since the last run) go first: they may be slow.
    longest = max(timings.values(), default=0.0) + 1
    names.sort(key=lambda name: -timings.get(name, longest))
    # MOBSTER_NO_DEVICES=1: wireless.devicectl, phone_io.files and mobster-wifi-pair raise instead of running
    # (native_helpers.devices_allowed), so no test reaches a phone even outside the sandbox.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", MOBSTER_NO_DEVICES="1")

    scratch = os.path.realpath(tempfile.mkdtemp(prefix="mobster-ci-"))
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, args.python, root, name, env, scratch) for name in names]
        for future in as_completed(futures):
            results.append(future.result())
    flaky = []
    for index, result in enumerate(results):
        if result["ok"]:
            continue
        retry = run(args.python, root, result["module"], env, os.path.join(scratch, "retry"))
        if retry["ok"]:
            flaky.append(result["module"])
            print(f"FLAKY {result['module']}: failed in parallel, passed alone. The first run:\n{result['output']}")
            results[index] = dict(retry, seconds=result["seconds"])
        else:
            print(f"FAIL {result['module']} ({result['seconds']:.1f}s, and again alone)\n{retry['output']}", flush=True)
    wall = time.monotonic() - started
    shutil.rmtree(scratch, ignore_errors=True)

    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    TIMINGS.write_text(json.dumps({r["module"]: round(r["seconds"], 2) for r in results}, indent=1, sort_keys=True))

    failed = [r["module"] for r in results if not r["ok"]]
    tests = sum(r["tests"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    busy = sum(r["seconds"] for r in results)
    slowest = sorted(results, key=lambda r: -r["seconds"])[:3]
    print(f"backend: {tests} tests in {len(results)} modules, {skipped} skipped, "
          f"{wall:.0f}s wall ({busy:.0f}s of work on {args.jobs} jobs)")
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

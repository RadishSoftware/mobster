"""`mobster sim doctor`: what `mobster verify` needs on this Mac, and the command that fixes each gap.

Each check is a `doctor.Check`-shaped dict {key, label, state, detail, fix}, state ok, warn, fail or skip. The
checks only read: `--fix` prepares a simulator (acquire, then release) and nothing more. They never run sudo
and never download a runtime; they print those commands. A key is reported as set or not set, never shown.
"""

import os
from pathlib import Path
import platform
import shutil

from .. import doctor as mobster_doctor
from .registry import SLOTS, free_slot, port_base, taken_ports

OK, WARN, FAIL, SKIP = mobster_doctor.OK, mobster_doctor.WARN, mobster_doctor.FAIL, mobster_doctor.SKIP
MIN_FREE_BYTES = 5 * 1024 ** 3
# Measured on 28 Sep (iOS 26.4, Xcode 26.4): a freshly booted iPhone 17 Pro simulator's folder is 1.6 GB, and
# the WebDriverAgent build 209 MB. Two simulators (MOBSTER_MAX_SIMS' default) and the build come to 3.4 GB.
DISK_FIX = "Free at least 5 GB: each booted simulator takes 1.6 GB, and the WebDriverAgent build 0.2 GB"
KEY_LABEL = "Smart key"
ENVIRONMENT = ("arch", "xcode", "first_launch", "runtime", "device_type", "git", "disk")


def check(key, label, state, detail, fix=""):
    return mobster_doctor.Check(key, label, state, detail, fix).public()


def _home(path):
    return mobster_doctor._home(path)


def check_arch(manager):
    machine = platform.machine()
    if machine != "arm64":
        translated = manager.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], timeout=5).out.strip() == "1"
        if not translated:
            return check("arch", "Apple silicon", FAIL, f"this Mac is {machine}",
                         "Mobster's simulators need a Mac with Apple silicon")
        return check("arch", "Apple silicon", WARN, "running under Rosetta",
                     "Run the arm64 build of mobster: brew reinstall mobster")
    return check("arch", "Apple silicon", OK, "arm64")


def check_xcode(manager):
    from .. import device_manager
    xcode, selected = device_manager.developer_dir()
    if not xcode:
        return check("xcode", "Xcode", FAIL, "not installed (the Command Line Tools alone can't run simulators)",
                     "Install Xcode from the App Store, then open it once")
    app = str(Path(xcode).parent.parent)
    env = {**os.environ, "DEVELOPER_DIR": xcode}
    result = manager.run([str(Path(xcode) / "usr" / "bin" / "xcodebuild"), "-version"], timeout=60, env=env)
    text = result.out + result.err
    if not result.ok:
        if "license" in text.lower():
            return check("xcode", "Xcode", FAIL, "its license isn't accepted", "sudo xcodebuild -license accept")
        return check("xcode", "Xcode", FAIL, f"xcodebuild failed: {result.last_line() or 'no output'}",
                     "Open Xcode once to finish its setup")
    from .api import parse_xcode_version
    version, build = parse_xcode_version(result.out)
    detail = f"Xcode {version} ({build}) at {_home(app)}"
    if selected != xcode:
        where = _home(selected) if selected else "nothing"
        return check("xcode", "Xcode", FAIL, f"{detail}, but xcode-select points at {where}",
                     f"sudo xcode-select -s {app}")
    return check("xcode", "Xcode", OK, detail)


def check_first_launch(manager):
    from .. import device_manager
    xcode, _ = device_manager.developer_dir()
    if not xcode:
        return check("first_launch", "Xcode components", SKIP, "needs Xcode")
    env = {**os.environ, "DEVELOPER_DIR": xcode}
    result = manager.run([str(Path(xcode) / "usr" / "bin" / "xcodebuild"), "-checkFirstLaunchStatus"],
                         timeout=60, env=env)
    if result.ok:
        return check("first_launch", "Xcode components", OK, "installed")
    return check("first_launch", "Xcode components", FAIL, "Xcode's first-launch components aren't installed",
                 "sudo xcodebuild -runFirstLaunch")


def check_catalog(manager):
    """The runtime and device type checks, from one `simctl list -j`."""
    from .api import SimError
    try:
        catalog = manager._catalog()
    except SimError as exc:
        return [check("runtime", "iOS runtime", FAIL, str(exc), exc.fix),
                check("device_type", "Device type", SKIP, "needs simctl")]
    from . import simctl
    try:
        runtime = simctl.choose_runtime(catalog[0])
    except SimError as exc:
        return [check("runtime", "iOS runtime", FAIL, str(exc), exc.fix),
                check("device_type", "Device type", SKIP, "needs an iOS runtime")]
    runtime_check = check("runtime", "iOS runtime", OK, runtime.name)
    try:
        device_type = simctl.choose_device_type(catalog[1], runtime)
    except SimError as exc:
        return [runtime_check, check("device_type", "Device type", FAIL, str(exc), exc.fix)]
    return [runtime_check, check("device_type", "Device type", OK, device_type.name)]


def check_git(manager, built):
    from .. import device_manager
    git = device_manager.xcode_tool("git")
    if git:
        return check("git", "git", OK, git)
    if built:
        return check("git", "git", SKIP, "not installed; not needed while WebDriverAgent is built")
    return check("git", "git", FAIL, "not installed; the first WebDriverAgent build downloads its source with it",
                 "Install Xcode, which includes git")


def _existing(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_disk(manager):
    folder = _existing(manager.data_dir)
    free = shutil.disk_usage(folder).free
    detail = f"{free / 1024 ** 3:.0f} GB free for {_home(manager.data_dir)}"
    if free < MIN_FREE_BYTES:
        return check("disk", "Free space", FAIL, detail, DISK_FIX)
    return check("disk", "Free space", OK, detail)


def wda_plan(manager):
    from .wda import build_dir, find_xctestrun
    try:
        tools = manager.tools()
    except Exception:
        return None
    return find_xctestrun(build_dir(manager.data_dir, tools.build))


def check_wda_build(manager):
    plan = wda_plan(manager)
    if plan:
        return check("wda_build", "WebDriverAgent", OK, f"built for this Xcode ({_home(plan.parents[2])})")
    return check("wda_build", "WebDriverAgent", WARN, "not built yet: the first run builds it, once per Xcode version",
                 "mobster sim doctor --fix")


def check_simulators(manager):
    rows = manager.list()
    if not rows:
        return check("simulators", "Simulators", SKIP, "none yet: the first run creates one")
    parts, stopped = [], []
    for row in rows:
        if row["state"] == "Booted":
            state = "booted, WebDriverAgent " + ("ready" if row["wda"] == "ready" else "stopped")
            if row["wda"] != "ready":
                stopped.append(row)
        else:
            state = row["state"].lower()
        parts.append(f"{row['name']} ({state}{', running a check' if row['in_use'] else ''})")
    detail = "; ".join(parts)
    if stopped:
        return check("simulators", "Simulators", WARN, detail, "mobster sim prepare")
    return check("simulators", "Simulators", OK, detail)


def check_ports(manager):
    base = port_base()
    entries = manager.registry.read()
    blocked = []
    for entry in entries:
        port = entry["wda_port"]
        if manager.wda.listening(port) and manager.wda.owner(port) != entry["udid"]:
            blocked.append(port)
    span = f"{base}–{base + SLOTS - 1}"
    try:
        wda_port, _ = free_slot(base, taken_ports(entries), busy=manager.wda.listening)
    except Exception as exc:
        return check("ports", "Ports", FAIL, str(exc), getattr(exc, "fix", ""))
    if blocked:
        return check("ports", "Ports", WARN, f"another program listens on {', '.join(map(str, blocked))}: the next "
                                             f"run moves that simulator to {wda_port}")
    return check("ports", "Ports", OK, f"WebDriverAgent on 127.0.0.1:{span}, MJPEG on +1000; {wda_port} is free")


def check_key():
    """Whether Smart has the key its model needs (SPEC §6.5, and #44 Smart on Claude). ``engines.smart_model``
    picks the model: MOBSTER_SMART_MODEL when set, else gpt-5.6-sol with an OpenAI key, else claude-sonnet-5-5
    with only an Anthropic key. ``engines.smart_key`` is the key that model runs on. Launch-only and key-less
    checks need no key, so a missing one is skip. A key is reported as set or not set, never shown."""
    from .. import engines
    model = engines.smart_model()
    provider = engines.provider_name(model)
    variable = "ANTHROPIC_API_KEY" if provider == "Anthropic" else "OPENAI_API_KEY"
    if engines.smart_key():
        return check("key", KEY_LABEL, OK, f"your {provider} key is set: Smart runs flows on {model}")
    none_needed = "not set: launch-only and key-less checks need none"
    if (os.environ.get("MOBSTER_SMART_MODEL") or "").strip() != model:
        return check("key", KEY_LABEL, SKIP, f"{none_needed}; Smart needs your OpenAI or Anthropic key",
                     "For Smart, put OPENAI_API_KEY=… or ANTHROPIC_API_KEY=… in an env file and pass --env-file PATH")
    # MOBSTER_SMART_MODEL chose the model, and its provider's key is missing.
    other, other_key = (("OpenAI", engines.openai_smart_key()) if provider == "Anthropic"
                        else ("Anthropic", engines.anthropic_key()))
    if not other_key:
        return check("key", KEY_LABEL, SKIP, f"{none_needed}; Smart on {model} needs your {provider} key",
                     f"For Smart, put {variable}=… in an env file and pass --env-file PATH")
    unset = {name: value for name, value in os.environ.items() if name != "MOBSTER_SMART_MODEL"}
    return check("key", KEY_LABEL, WARN, f"MOBSTER_SMART_MODEL is {model}, which needs {variable}; only your {other} "
                                         "key is set, so Smart can't run",
                 f"Add {variable}=… to your env file, or remove MOBSTER_SMART_MODEL to run Smart on "
                 f"{engines.smart_model(unset)}")


def _safe(key, label, function, *args):
    try:
        return function(*args)
    except Exception as exc:  # a doctor reports; it never crashes
        return check(key, label, FAIL, f"the check failed: {exc}")


def run_checks(manager, fix=False):
    checks = [_safe("arch", "Apple silicon", check_arch, manager),
              _safe("xcode", "Xcode", check_xcode, manager),
              _safe("first_launch", "Xcode components", check_first_launch, manager)]
    xcode_ok = checks[1]["state"] == OK
    if xcode_ok:
        try:
            checks += check_catalog(manager)
        except Exception as exc:
            checks += [check("runtime", "iOS runtime", FAIL, f"the check failed: {exc}"),
                       check("device_type", "Device type", SKIP, "needs simctl")]
    else:
        checks += [check("runtime", "iOS runtime", SKIP, "needs Xcode"),
                   check("device_type", "Device type", SKIP, "needs Xcode")]
    checks.append(_safe("git", "git", check_git, manager, bool(xcode_ok and wda_plan(manager))))
    checks.append(_safe("disk", "Free space", check_disk, manager))
    if fix:
        checks.append(_prepare(manager, [item for item in checks if item["state"] == FAIL]))
    if xcode_ok:
        checks.append(_safe("wda_build", "WebDriverAgent", check_wda_build, manager))
        checks.append(_safe("simulators", "Simulators", check_simulators, manager))
        checks.append(_safe("ports", "Ports", check_ports, manager))
    checks.append(_safe("key", KEY_LABEL, check_key))
    return checks


def _prepare(manager, failures):
    from .api import SimError
    if failures:
        return check("prepare", "Prepare", SKIP, "fix the problems above first")
    try:
        lease = manager.acquire()
    except SimError as exc:
        return check("prepare", "Prepare", FAIL, str(exc), exc.fix)
    try:
        target, seconds = lease.target, lease.timing.get("total", 0)
    finally:
        lease.release()
    return check("prepare", "Prepare", OK, f"{target.name} is booted, with WebDriverAgent on "
                                           f"{target.wda_url.split('//')[-1]} ({seconds:.1f} s)")


def problems(checks):
    return [item for item in checks if item["state"] == FAIL]


def report(checks, color=False):
    """One line per check, the fix under each problem, then a verdict line."""
    paint = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[2m"} if color else {}
    dim, reset = ("\033[2m", "\033[0m") if color else ("", "")
    lines = ["Mobster sim doctor", ""]
    for item in checks:
        glyph = f"{paint.get(item['state'], '')}{mobster_doctor.GLYPHS[item['state']]}{reset if color else ''}"
        lines.append(f"  {glyph} {item['label']:<17} {item['detail']}")
        if item["fix"] and item["state"] in {WARN, FAIL}:
            lines.append(f"    {'':<17} {dim}→ {item['fix']}{reset}")
    failed = problems(checks)
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} problem{'s' if len(failed) != 1 else ''} to fix before `mobster verify` can run.")
    else:
        lines.append("Ready for `mobster verify`.")
    return "\n".join(lines)

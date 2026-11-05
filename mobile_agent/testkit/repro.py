"""A crash report becomes a check that reproduces it (docs/dev/crashes.mdx).

    intake   a .ips, a Sentry event, a TestFlight or Xcode crash log, or a brief  ->  Brief
    check    the brief's path through the app                                      ->  .mobster/checks/repro-<id>.yaml
    judge    the run of that check                                                 ->  Outcome

A crash report is data from outside. Its breadcrumb text is never copied into a step as written: each step comes from a
fixed template with the label quoted, cut short and redacted. The run is a plain check, so a crash fails it as
``app_crashed`` and a fix makes it pass. Mobster merges nothing."""

import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import secret_filter

CRASHES_DIR = "crashes"
STEP_LIMIT, LABEL_LIMIT, FRAME_LIMIT, NAME_LIMIT = 20, 60, 12, 110
SOURCES = ("ips", "sentry", "text", "brief")
CLICKS = frozenset({"ui.click", "ui.tap", "ui.action", "touch", "tap", "click"})
STATUSES = ("reproduced", "different_crash", "not_reproduced", "couldnt_run")
SAME_SIGNAL = (frozenset({"EXC_BREAKPOINT", "SIGTRAP"}), frozenset({"EXC_BAD_ACCESS", "SIGSEGV", "SIGBUS"}),
               frozenset({"EXC_CRASH", "SIGABRT"}), frozenset({"EXC_BAD_INSTRUCTION", "SIGILL"}))


class ReproError(Exception):
    """Why a crash can't be taken in or run: one sentence, then what to do."""


@dataclass
class Brief:
    source: str
    bundle: str = ""
    version: str = ""
    device: str = ""
    os: str = ""
    type: str = ""            # EXC_BREAKPOINT, or the exception class a Sentry event names
    signal: str = ""
    message: str = ""
    frames: list = field(default_factory=list)   # {symbol, file, line, image, own}, the crashed thread, top first
    steps: list = field(default_factory=list)    # one sentence each, from fixed templates
    link: str = ""            # a link that opens the app on the crashing screen, when the crash names one

    @property
    def id(self):
        own = top_own(self.frames)
        key = "|".join((self.bundle, self.type or self.signal, own.casefold() or self.message[:80]))
        return hashlib.sha1(key.encode()).hexdigest()[:8]

    @property
    def headline(self):
        from ..verify.crashes import headline
        return headline({"message": self.message, "type": self.type, "signal": self.signal, "frames": self.frames},
                        self.bundle.rsplit(".", 1)[-1] or "The app")

    def public(self):
        return {"id": self.id, "source": self.source, "bundle": self.bundle, "version": self.version,
                "device": self.device, "os": self.os, "type": self.type, "signal": self.signal,
                "message": self.message, "frames": self.frames[:FRAME_LIMIT], "steps": list(self.steps),
                "link": self.link, "headline": self.headline}


def base_name(symbol):
    """'static Daybreak.PlantedCrash.unwrap() -> ()' -> 'unwrap': the function, without keywords, module, type,
    arguments or offset, so a Sentry name and a crash report's demangled one compare equal."""
    head = str(symbol or "").split("(", 1)[0].split()
    return head[-1].rsplit(".", 1)[-1] if head else ""


def _text(value, limit):
    return secret_filter.redact(" ".join(str(value or "").split()))[:limit]


def _label(value):
    """A breadcrumb's label as a step may quote it: one line, no quote marks, cut short, redacted."""
    text = _text(value, LABEL_LIMIT * 2).replace("“", "").replace("”", "").replace('"', "").replace("`", "")
    return text[:LABEL_LIMIT].strip()


# -- intake --------------------------------------------------------------------------------------------------------

def load(path):
    """The brief for the crash file at ``path``."""
    return from_content(*detect(path))


def from_content(kind, content):
    """The brief for a crash given as ``kind`` (one of SOURCES) and its content: a path, a dict or text."""
    if kind not in SOURCES:
        raise ReproError(f"A crash comes from one of {', '.join(SOURCES)}, not {kind!r}.")
    return {"ips": from_ips, "sentry": from_sentry, "text": from_text, "brief": from_brief}[kind](content)


def detect(path):
    """(kind, content) of the crash file at ``path``: .ips, JSON that is a Sentry event or a brief, else a crash log."""
    path = Path(path).expanduser()
    try:
        raw = path.read_bytes()[:4_000_000].decode("utf-8", "replace")
    except OSError as error:
        raise ReproError(f"Can't read {path} ({error.strerror or type(error).__name__}). Check the path.") from None
    if path.suffix == ".ips":
        return "ips", path
    try:
        data = json.loads(raw)
    except ValueError:
        return "text", raw
    if isinstance(data, dict) and ("steps" in data or "frames" in data) and "exception" not in data \
            and "entries" not in data:
        return "brief", data
    if isinstance(data, dict):
        return "sentry", data
    raise ReproError(f"{path.name} is JSON but not a Sentry event or a brief. See docs/dev/crashes.mdx.")


def from_ips(path):
    from ..verify import crashes
    report = crashes.read(path)
    if report is None:
        raise ReproError(f"{Path(path).name} isn't an app crash report (.ips with bug_type 309). Export it again "
                         "from Xcode's Organizer or the device's crash logs.")
    summary = crashes.summary(report, executable=report.process)
    header = report.header
    info = report.body.get("bundleInfo") or {}
    return Brief("ips", bundle=report.bundle_id or "", version=_text(info.get("CFBundleShortVersionString"), 40),
                 device=_text(report.body.get("modelCode"), 40), os=_text(header.get("os_version"), 60),
                 type=summary.get("type") or "", signal=summary.get("signal") or "",
                 message=_text(summary.get("message"), 300), frames=_frames(summary.get("frames")))


def _frames(frames):
    return [{"symbol": _text(f.get("symbol"), 200) or None, "file": _text(f.get("file"), 120) or None,
             "line": f.get("line") if isinstance(f.get("line"), int) else None, "image": _text(f.get("image"), 80),
             "own": bool(f.get("own"))} for f in (frames or ())[:FRAME_LIMIT] if isinstance(f, dict)]


FRAME_LINE = re.compile(r"^\d+\s+(?P<image>\S+)\s+0x[0-9a-fA-F]+\s+(?P<symbol>.+?)(?:\s+\+\s+\d+)?"
                        r"(?:\s+\((?P<file>[^():]+):(?P<line>\d+)\))?\s*$")


def from_text(text):
    """A TestFlight or Xcode Organizer crash log in Apple's text format."""
    def field_(name):
        found = re.search(rf"^{name}:\s*(.+)$", text, re.M)
        return found.group(1).strip() if found else ""
    process = re.sub(r"\s*\[\d+\]$", "", field_("Process"))
    kind = re.match(r"(\w+)(?:\s+\((\w+)\))?", field_("Exception Type"))
    if not process or not kind:
        raise ReproError("This isn't a crash log Mobster reads (no Process and Exception Type lines). Pass the "
                         ".crash file from TestFlight (`mobster asc feedback --crashes --save DIR`) or Xcode.")
    stack = re.search(r"^Thread \d+ Crashed:.*?\n(.*?)(?:\n\s*\n|\Z)", text, re.M | re.S)
    frames = []
    for line in (stack.group(1).splitlines() if stack else ())[:FRAME_LIMIT]:
        found = FRAME_LINE.match(line.strip())
        if found:
            image = found.group("image")
            frames.append({"symbol": _text(found.group("symbol"), 200), "file": found.group("file"),
                           "line": int(found.group("line")) if found.group("line") else None, "image": image,
                           "own": image == process})
    reason = field_("Termination Reason") or field_("Application Specific Information")
    return Brief("text", bundle=field_("Identifier"), version=field_("Version"), device=field_("Hardware Model"),
                 os=field_("OS Version"), type=kind.group(1), signal=kind.group(2) or "", message=_text(reason, 300),
                 frames=frames)


def from_sentry(event):
    """A Sentry event as the web API or Sentry's MCP returns it: exception, breadcrumbs, device and release."""
    if not isinstance(event, dict):
        raise ReproError("A Sentry event is a JSON object. Pass the event, not the issue list.")
    entries = {item.get("type"): item.get("data") for item in event.get("entries") or () if isinstance(item, dict)}
    values = ((event.get("exception") or entries.get("exception") or {}).get("values")) or []
    crumbs = ((event.get("breadcrumbs") or entries.get("breadcrumbs") or {}).get("values")) or []
    if not values or not isinstance(values[-1], dict):
        raise ReproError("This Sentry event has no exception. Pass an event from a crash.")
    error = values[-1]
    meta = ((error.get("mechanism") or {}).get("meta")) or {}
    mach = (meta.get("mach_exception") or {}).get("name") or ""
    signal = (meta.get("signal") or {}).get("name") or ""
    contexts = event.get("contexts") or {}
    app, device, os_ = (contexts.get(key) or {} for key in ("app", "device", "os"))
    release = str(event.get("release") or "")
    bundle = app.get("app_identifier") or (release.split("@", 1)[0] if "@" in release else "")
    frames = []
    for frame in reversed(((error.get("stacktrace") or {}).get("frames")) or []):  # Sentry lists the crash last
        if isinstance(frame, dict):
            frames.append({"symbol": _text(frame.get("function") or frame.get("symbol"), 200) or None,
                           "file": _text(frame.get("filename"), 120) or None,
                           "line": frame.get("lineno") if isinstance(frame.get("lineno"), int) else None,
                           "image": _text(frame.get("package") or frame.get("module"), 80),
                           "own": bool(frame.get("in_app"))})
    request_url = str((event.get("request") or {}).get("url") or "")
    return Brief("sentry", bundle=_text(bundle, 255), version=_text(app.get("app_version"), 40),
                 device=_text(device.get("model") or device.get("model_id"), 60),
                 os=_text(f"{os_.get('name', '')} {os_.get('version', '')}", 60),
                 type=mach or _text(error.get("type"), 80), signal=signal, message=_text(error.get("value"), 300),
                 frames=frames[:FRAME_LIMIT], steps=steps_from_breadcrumbs(crumbs),
                 link=request_url if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", request_url)
                 and not request_url.startswith(("http://", "https://")) else "")


def steps_from_breadcrumbs(crumbs):
    """What the person did, nearest the crash last: taps and screens, each from a fixed sentence."""
    steps = []
    for crumb in crumbs:
        if not isinstance(crumb, dict):
            continue
        data = crumb.get("data") if isinstance(crumb.get("data"), dict) else {}
        category = str(crumb.get("category") or "").casefold()
        if category in CLICKS:
            label = _label(data.get("label") or data.get("title") or data.get("view.id") or crumb.get("message"))
            step = f"Tap “{label}”" if label else None
        elif category == "navigation":
            label = _label(data.get("to") or crumb.get("message"))
            step = f"Go to the “{label}” screen" if label else None
        else:
            step = None
        if step and (not steps or steps[-1] != step):
            steps.append(step)
    return steps[-STEP_LIMIT:]


def from_brief(data):
    """A brief written by hand or by an agent from Crashlytics, Datadog or anything else: the fields of Brief."""
    if not isinstance(data, dict):
        raise ReproError("A brief is a JSON object with bundle, type, frames and steps.")
    raw = data.get("steps") or []
    steps = [_text(step, 200) for step in raw if isinstance(step, str) and step.strip()][:STEP_LIMIT]
    return Brief("brief", bundle=_text(data.get("bundle"), 255), version=_text(data.get("version"), 40),
                 device=_text(data.get("device"), 60), os=_text(data.get("os"), 60), type=_text(data.get("type"), 80),
                 signal=_text(data.get("signal"), 20), message=_text(data.get("message"), 300),
                 frames=_frames(data.get("frames")), steps=steps, link=_text(data.get("link"), 2000))


# -- the check ----------------------------------------------------------------------------------------------------

def check_data(brief, *, bundle=None, app=None, args=(), link=None):
    """The check file's shape for ``brief``. Raises ReproError when the app can't be named."""
    from ..verify.checks import LIMITS, CheckError, check_from_dict
    app_block = {"path": str(app)} if app else {"bundle": bundle or brief.bundle}
    if not app and not app_block["bundle"]:
        raise ReproError("The crash doesn't name the app. Pass the bundle ID (bundle) or a built .app (app).")
    where = top_own(brief.frames)
    name = f"Repro {brief.id}: " + (f"crash in {where}" if where else brief.headline)
    data = {"version": 1, "name": name[:NAME_LIMIT - 1] + "…" if len(name) > NAME_LIMIT else name,
            "tags": ["repro"], "app": app_block}
    launch = {key: value for key, value in (("url", link or brief.link), ("args", list(args))) if value}
    if launch:
        data["launch"] = launch
    if brief.steps:
        data["steps"] = brief.steps[-LIMITS["steps"]:]
    # Nothing decides a run with no expectation (needs review), and "no crash" isn't a thing a screen shows: the crash
    # fails the run as app_crashed first, so this holds on any live screen and a fixed app passes.
    data["expect"] = [{"count": {"label": "/./"}, "at_least": 1, "name": "The app is still showing a screen"}]
    try:
        check_from_dict(data)
    except CheckError as error:
        raise ReproError(f"The crash makes a check that doesn't load ({error}). Fix the brief.") from None
    return data


def write(brief, root, **options):
    """Write the brief and its check under ``root``/.mobster. A check that exists is kept: it may be edited.
    (check path, brief path, whether the check was already there)"""
    import yaml
    root = Path(root)
    check = root / ".mobster" / "checks" / f"repro-{brief.id}.yaml"
    folder = root / ".mobster" / CRASHES_DIR / brief.id
    data = check_data(brief, **options)
    folder.mkdir(parents=True, exist_ok=True)
    (root / ".mobster" / CRASHES_DIR / ".gitignore").write_text("*\n", encoding="utf-8")  # evidence stays local
    (folder / "brief.json").write_text(json.dumps(brief.public(), indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    existed = check.is_file()
    if not existed:
        check.parent.mkdir(parents=True, exist_ok=True)
        header = (f"# Reproduces a crash from {brief.source}: {brief.headline}\n"
                  f"# Run it with: mobster test {check.relative_to(root)}\n"
                  "# It fails while the app crashes and passes once it doesn't. Commit it with the fix.\n")
        check.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
                         encoding="utf-8")
    return check, folder / "brief.json", existed


# -- the verdict --------------------------------------------------------------------------------------------------

def same_kind(brief, crash):
    wanted = {str(brief.type).upper(), str(brief.signal).upper()} - {""}
    got = {str(crash.get("type")).upper(), str(crash.get("signal")).upper()} - {"", "NONE"}
    if not wanted or not got:
        return True   # the brief doesn't say how it died: the stack decides
    return bool(wanted & got) or any(wanted & group and got & group for group in SAME_SIGNAL)


def top_own(frames):
    return next((base_name(f["symbol"]) for f in frames or () if f.get("own") and f.get("symbol")), "")


def match(brief, crash):
    """(True, how) when ``crash`` (the run's evidence) is the crash the brief describes."""
    if not same_kind(brief, crash):
        return False, f"It crashed with {crash.get('type') or crash.get('signal')}, not {brief.type or brief.signal}."
    wanted, got = top_own(brief.frames), top_own(crash.get("frames"))
    if wanted and got:
        if wanted.casefold() == got.casefold():
            return True, f"Same exception, same function ({wanted})."
        return False, f"It crashed in {got}, not {wanted}."
    return True, "Same exception; the stack had no function to compare."


def outcome(brief, doc, *, check, folder=None):
    """The reproduction's result from the suite document of the run of ``check``."""
    results = [r for item in doc.get("checks") or () for r in item.get("results") or ()]
    result = results[0] if results else {}
    steps = [f"Launched {brief.bundle or 'the app'}" + (f" at {brief.link}" if brief.link else "")] + list(brief.steps)
    crash = (result.get("evidence") or {}).get("crash")
    reason = result.get("reason") or {}
    out = {"id": brief.id, "check": str(check), "headline": brief.headline, "stepsTried": steps, "crash": None,
           "video": None, "report": result.get("report"), "note": ""}
    if result.get("status") == "failed" and reason.get("class") == "app_crashed" and crash:
        out["crash"] = {key: crash.get(key) for key in ("type", "signal", "message", "frames", "symbolicated")}
        ok, why = match(brief, crash)
        out["status"], out["note"] = ("reproduced" if ok else "different_crash"), why
    elif result.get("status") == "passed":
        out["status"] = "not_reproduced"
        out["note"] = "The app ran these steps and didn't crash. Mobster didn't guess further."
    else:
        out["status"] = "couldnt_run"
        out["note"] = reason.get("message") or "The run ended before it could tell. See the report."
    if result.get("video") and folder:
        out["video"] = str(Path(folder) / result["video"])
    return out


def save(brief, root, result):
    """Keep the outcome beside the brief, with the video copied in; returns the outcome with its saved paths."""
    folder = Path(root) / ".mobster" / CRASHES_DIR / brief.id
    folder.mkdir(parents=True, exist_ok=True)
    video = result.get("video")
    if video and Path(video).is_file():
        shutil.copyfile(video, folder / "repro.mp4")
        result["video"] = str(folder / "repro.mp4")
    result = {**result, "ranAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (folder / "repro.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                                       encoding="utf-8")
    return result


def summary_lines(brief, result):
    """What the coding agent or the person reads first."""
    status = result["status"]
    head = {"reproduced": f"Reproduced: {brief.headline}",
            "different_crash": f"Crashed, but not the same way: {brief.headline}",
            "not_reproduced": f"Couldn't reproduce: {brief.headline}",
            "couldnt_run": f"Couldn't run the reproduction: {brief.headline}"}[status]
    lines = [head, result["note"], "Steps tried:"] + [f"  {n}. {step}"
                                                      for n, step in enumerate(result["stepsTried"], 1)]
    if result.get("crash"):
        top = next((f for f in result["crash"].get("frames") or () if f.get("own") and f.get("symbol")), None)
        if top:
            from ..verify.crashes import frame_text
            lines.append(f"It crashed in {frame_text(top, image=False)}.")
    if result.get("video"):
        lines.append(f"Video: {result['video']}")
    lines.append(f"Check: {result['check']}")
    if status == "reproduced":
        lines.append("Fix the app, rebuild, then run the check again (run_tests with this path, or `mobster test "
                     "PATH`): it passes once the app no longer crashes. Mobster merges nothing; the pull request is "
                     "the person's to merge.")
    return lines


def add_arguments(parser):
    """`mobster test repro`'s own flags, as their own help group."""
    group = parser.add_argument_group("mobster test repro")
    group.add_argument("--arg", dest="repro_arg", action="append", metavar="ARG",
                       help="a launch argument the crash needs, such as -MyFlag; repeatable (--bundle and --app name "
                            "the app)")
    group.add_argument("--link", metavar="URL", help="a link that opens the app on the crashing screen")


def main(args, rest):
    """`mobster test repro` from the command line: ctrl+c stops it with exit 130 and the usual line."""
    from ..devtools import signals_as_interrupt
    with signals_as_interrupt():
        try:
            return command(args, rest)
        except KeyboardInterrupt:
            print("mobster test: stopped", file=sys.stderr, flush=True)
            return 130


def command(args, rest, *, out=None, err=None, cwd=None, plan=None, execute=None):
    """`mobster test repro CRASH_FILE`: a crash report becomes a check, run on a simulator with the screen recorded.
    Exit 0 reproduced, 1 not reproduced or a different crash, 3 couldn't run."""
    from ..verify.checks import project_root
    from .api import Options, UsageError
    from .cli import USAGE_EXIT, _command, _relative, _usage
    out, err = out or sys.stdout, err or sys.stderr
    if len(rest) != 1:
        return _usage("repro needs the crash file: mobster test repro crash.ips (a Sentry event .json, a TestFlight "
                      ".crash or a brief .json also work)")
    root = project_root(Path(cwd or os.getcwd()))
    try:
        brief = load(Path(rest[0]).expanduser())
        check, _, existed = write(brief, root, bundle=getattr(args, "bundle", None),
                                  app=getattr(args, "app", None), args=tuple(args.repro_arg or ()),
                                  link=getattr(args, "link", None))
    except ReproError as error:
        return _usage(str(error))
    print(f"{'Using' if existed else 'Wrote'} {_relative(check, root)}", file=err, flush=True)
    options = Options(paths=(str(check),), sims=tuple(args.sim or ()), retries=0, video=True,
                      keyless=bool(getattr(args, "keyless", False)), command=_command(args))
    if plan is None:
        from .api import plan
    if execute is None:
        from .api import execute
    try:
        planned = plan(options)
    except UsageError as error:
        return _usage(str(error))
    doc, paths = execute(planned, progress=lambda line: print(line, file=err, flush=True))
    result = save(brief, root, outcome(brief, doc, check=_relative(check, root), folder=paths.get("folder")))
    if getattr(args, "json", False):
        print(json.dumps({"brief": brief.public(), **result}, ensure_ascii=False), file=out, flush=True)
    else:
        print("\n".join(summary_lines(brief, result)), file=out, flush=True)
    return {"reproduced": 0, "different_crash": 1, "not_reproduced": 1}.get(result["status"], USAGE_EXIT)

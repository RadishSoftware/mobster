import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import secret_filter

DIAGNOSTIC_REPORTS = Path.home() / "Library" / "Logs" / "DiagnosticReports"
CRASH_BUG_TYPE = "309"
TOP_FRAMES = 8
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_DSYMS = 24
TOOL_TIMEOUT = 15
MESSAGE_LIMIT = 400
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z")
ATOS_LINE = re.compile(r"^(?P<symbol>.+?) \(in (?P<image>[^)]+)\)(?: \((?P<file>[^():]+):(?P<line>\d+)\))?\s*$")
SYSTEM_PREFIXES = ("/System/", "/usr/lib/", "/Library/Developer/CoreSimulator/Volumes/", "/Applications/Xcode")


@dataclass
class Report:
    path: Path
    header: dict
    body: dict

    @property
    def bundle_id(self):
        return self.header.get("bundleID") or (self.body.get("bundleInfo") or {}).get("CFBundleIdentifier")

    @property
    def process(self):
        return self.body.get("procName") or self.header.get("name") or self.header.get("app_name")

    @property
    def pid(self):
        pid = self.body.get("pid")
        return pid if isinstance(pid, int) and not isinstance(pid, bool) else None

    @property
    def wall(self):
        """The crash time as seconds since the epoch: the body's captureTime, else the header's timestamp."""
        return parse_time(self.body.get("captureTime")) or parse_time(self.header.get("timestamp"))


def parse_time(text):
    if not isinstance(text, str):
        return None
    for form in TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), form).timestamp()
        except ValueError:
            continue
    return None


def read(path):
    """The crash report at ``path``, or None when it isn't one an app crash wrote (or can't be read)."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first, _, rest = text.partition("\n")
    try:
        header, body = json.loads(first), json.loads(rest)
    except ValueError:
        return None
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    if str(header.get("bug_type")
           or body.get("bug_type")) != CRASH_BUG_TYPE or not isinstance(body.get("threads"), list):
        return None
    return Report(path, header, body)


def candidates(folder, executable, since):
    """The .ips files in ``folder`` (and its Retired folder) named for ``executable`` and changed since ``since`` (epoch
    seconds), newest first."""
    out = []
    for directory in (Path(folder), Path(folder) / "Retired"):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.name.startswith(f"{executable}-") or not path.name.endswith(".ips"):
                continue
            try:
                changed = path.stat().st_mtime
            except OSError:
                continue
            if changed >= since:
                out.append((changed, path))
    return [path for _, path in sorted(out, reverse=True)]


def belongs(report, *, bundle_id, executable, since, until=None, pids=(), udid=None):
    # Two simulators running the same app never take each other's crash, nor a second crash on the same one.
    named = report.bundle_id == bundle_id if report.bundle_id else (executable and report.process == executable)
    if not named:
        return False
    wall = report.wall
    if wall is None or wall < since or (until is not None and wall > until):
        return False
    if pids:
        return report.pid is not None and report.pid in set(pids)
    if udid:
        where = f"{report.body.get('procPath') or ''} {report.body.get('coalitionName') or ''}"
        return udid in where
    return True


def _thread(body):
    threads = body.get("threads") or []
    index = body.get("faultingThread")
    if isinstance(index, int) and 0 <= index < len(threads) and isinstance(threads[index], dict):
        return threads[index]
    return next((thread for thread in threads if isinstance(thread, dict) and thread.get("triggered")), None)


def message(body):
    asi = body.get("asi")
    if isinstance(asi, dict):
        for lines in asi.values():
            for line in lines if isinstance(lines, list) else [lines]:
                if isinstance(line, str) and line.strip():
                    return _clean(line)
    termination = body.get("termination") or {}
    for key in ("details", "reasons"):
        value = termination.get(key)
        if isinstance(value, list) and value and isinstance(value[0], str):
            return _clean(value[0])
    return None


def _clean(text):
    text = " ".join(str(text).split())
    return text[:MESSAGE_LIMIT]


def frames(report, top=TOP_FRAMES, executable=None):
    body = report.body
    thread = _thread(body) or {}
    images = body.get("usedImages") or []
    out = []
    for index, frame in enumerate((thread.get("frames") or [])[:top]):
        if not isinstance(frame, dict):
            continue
        image_index = frame.get("imageIndex")
        image = images[image_index] if isinstance(image_index, int) and 0 <= image_index < len(images) else {}
        base, offset = image.get("base"), frame.get("imageOffset")
        out.append({"index": index, "image": image.get("name") or "???", "own": _own_image(image, executable),
                    "symbol": frame.get("symbol"),
                    "offset": frame.get("symbolLocation"), "file": frame.get("sourceFile"),
                    "line": frame.get("sourceLine"), "imageIndex": image_index,
                    "address": base + offset if isinstance(base, int) and isinstance(offset, int) else None})
    return out


def summary(report, *, executable=None, dsyms=(), run=subprocess.run, top=TOP_FRAMES):
    """{type, signal, subtype, message, thread, frames, symbolicated}: what a person needs to see first."""
    body = report.body
    exception = body.get("exception") or {}
    thread = _thread(body) or {}
    stack = frames(report, top, executable or report.process)
    symbolicated = symbolicate(stack, body.get("usedImages") or [], dsyms, executable=executable, run=run)
    demangle(stack, run=run)
    for frame in stack:
        frame.pop("imageIndex", None)
        frame.pop("address", None)
    return {"type": exception.get("type"), "signal": exception.get("signal"), "subtype": exception.get("subtype"),
            "message": message(body), "thread": thread.get("queue") or thread.get("name"),
            "process": report.process, "pid": report.pid, "frames": stack, "symbolicated": symbolicated}


def headline(summary_, app_name):
    """One sentence for the verdict: what crashed, how, and where in the app's own code."""
    what = summary_.get("message") or " ".join(filter(None, [summary_.get("type"), f"({summary_['signal']})"
                                                              if summary_.get("type") and summary_.get("signal")
                                                              else summary_.get("signal")]))
    where = first_own_frame(summary_)
    text = f"{app_name} crashed" + (f": {what}" if what else "")
    if where:
        text += f", in {where}"
    return text.rstrip(".") + "."


def first_own_frame(summary_):
    """'CrashTrigger.crashNow() (ContentView.swift:23)': the first frame in the app's own image, else None."""
    for frame in summary_.get("frames") or ():
        if frame.get("own") and frame.get("symbol"):
            return frame_text(frame, image=False)
    return None


def frame_text(frame, image=True):
    symbol = frame.get("symbol") or (f"0x{frame['address']:x}" if frame.get("address") else "???")
    where = f" ({frame['file']}:{frame['line']})" if frame.get("file") and frame.get("line") else ""
    return (f"{frame.get('image')}  " if image else "") + symbol + where


def find_dsyms(app_path=None, root=None):
    found = []
    if app_path:
        app = Path(app_path)
        found.append(app.parent / f"{app.name}.dSYM")
    if root:
        products = Path(root) / ".mobster" / "build" / "Build" / "Products"
        try:
            for config in sorted(products.iterdir()):
                found.extend(sorted(config.glob("*.dSYM")) if config.is_dir() else [])
        except OSError:
            pass
    unique = []
    for path in found:
        if path.is_dir() and path not in unique:
            unique.append(path)
    return unique[:MAX_DSYMS]


def _uuid(text):
    return re.sub(r"[^0-9A-F]", "", str(text or "").upper())


def dsym_uuids(dsym, run=subprocess.run):
    """{UUID (hex, upper case, no dashes): the DWARF file inside the dSYM} from ``dwarfdump --uuid``."""
    try:
        done = run(["/usr/bin/xcrun", "dwarfdump", "--uuid", str(dsym)], capture_output=True, text=True,
                   timeout=TOOL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in (done.stdout or "").splitlines():
        match = re.match(r"UUID: ([0-9A-Fa-f-]+) \(\S+\) (.+)$", line.strip())
        if match:
            out[_uuid(match.group(1))] = match.group(2)
    return out


def _own_image(image, executable):
    path = str(image.get("path") or "")
    if executable and image.get("name") == executable:
        return True
    return ".app/" in path and not path.startswith(SYSTEM_PREFIXES)


def symbolicate(stack, images, dsyms, *, executable=None, run=subprocess.run):
    """Name the app's own frames with ``atos`` and a dSYM whose UUID matches their image."""
    wanted = {}
    for frame in stack:
        index = frame.get("imageIndex")
        if not isinstance(index, int) or not 0 <= index < len(images) or frame.get("address") is None:
            continue
        if frame.get("own") and not (frame.get("symbol") and frame.get("file")):
            wanted.setdefault(index, []).append(frame)
    if not wanted or not dsyms:
        return False
    files = {}
    for dsym in dsyms:
        files.update(dsym_uuids(dsym, run=run))
    named = False
    for index, todo in wanted.items():
        image = images[index]
        dwarf = files.get(_uuid(image.get("uuid")))
        base = image.get("base")
        if not dwarf or not isinstance(base, int):
            continue
        argv = ["/usr/bin/xcrun", "atos", "-arch", str(image.get("arch") or "arm64"), "-o", dwarf, "-l", hex(base)]
        argv += [hex(frame["address"]) for frame in todo]
        try:
            done = run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        for frame, line in zip(todo, (done.stdout or "").splitlines()):
            match = ATOS_LINE.match(line.strip())
            if not match:
                continue
            frame["symbol"] = match.group("symbol")
            if match.group("file"):
                frame["file"], frame["line"] = match.group("file"), int(match.group("line"))
            frame["offset"] = None
            named = True
    return named


def demangle(stack, run=subprocess.run):
    """Swift's mangled names ($s…) made readable with ``swift-demangle``, in place; the rest stay."""
    mangled = [frame for frame in stack if str(frame.get("symbol") or "").lstrip("_").startswith("$s")]
    if not mangled:
        return
    try:
        done = run(["/usr/bin/xcrun", "swift-demangle", "--compact", "--simplified"],
                   input="\n".join(frame["symbol"] for frame in mangled), capture_output=True, text=True,
                   timeout=TOOL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return
    lines = (done.stdout or "").splitlines()
    if len(lines) == len(mangled):
        for frame, line in zip(mangled, lines):
            if line.strip():
                frame["symbol"] = line.strip()


def write_copy(report, folder):
    # Every string goes through the secret filter; numbers, such as frame offsets, are kept as they are.
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / report.path.name
    header = secret_filter.redact_deep(report.header)
    body = secret_filter.redact_deep(report.body)
    target.write_text(json.dumps(header, ensure_ascii=False) + "\n" + json.dumps(body, indent=2, ensure_ascii=False)
                      + "\n", encoding="utf-8")
    return target


__all__ = ["Report", "read", "candidates", "belongs", "summary", "headline", "frame_text", "find_dsyms",
           "symbolicate", "write_copy", "DIAGNOSTIC_REPORTS"]

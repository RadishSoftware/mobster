"""logcat and crashes. Every line passes the secret filter before it is kept."""

import re
from dataclasses import dataclass

from .. import secret_filter
from .adb import Adb, valid_package

THREADTIME = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*:\s?(.*)$")
FATAL = re.compile(r"^FATAL EXCEPTION: (.*)$")
PROCESS = re.compile(r"^Process: ([A-Za-z0-9_.]+), PID: (\d+)")
ANR = re.compile(r"^ANR in ([A-Za-z0-9_.]+)(?: \(([^)]*)\))?")
EXCEPTION = re.compile(r"^([A-Za-z0-9_.$]+(?:Exception|Error)): ?(.*)$")


@dataclass(frozen=True)
class Crash:
    kind: str       # "crash" or "anr"
    package: str
    pid: int
    thread: str
    exception: str  # the first exception line, e.g. "java.lang.IllegalStateException: boom"
    lines: tuple    # the block's lines, filtered


def read_logcat(adb: Adb, since=None, limit=4000):
    """Filtered logcat lines (`adb logcat -d -v threadtime`), newest ``limit`` of them; ``since`` is
    "MM-DD HH:MM:SS.mmm"."""
    args = ["logcat", "-d", "-v", "threadtime", *(["-T", since] if since else [])]
    text = adb.adb(*args, timeout=30).text
    return [secret_filter.redact(line) for line in text.splitlines()[-limit:]]


def parse_crashes(lines, package=None):
    """The crashes and ANRs in threadtime lines. ``package`` keeps only that app's."""
    crashes, block, kind = [], [], None
    last_key = None

    def close():
        nonlocal block, kind
        if block:
            crash = _crash(kind, block)
            if crash and (package is None or crash.package == package):
                crashes.append(crash)
        block, kind = [], None

    for raw in lines:
        match = THREADTIME.match(raw)
        if not match:
            continue
        _time, pid, _tid, level, tag, message = match.groups()
        key = (pid, tag)
        if kind and (key != last_key or level not in {"E", "F"}):
            close()
        if not kind:
            if tag == "AndroidRuntime" and FATAL.match(message):
                kind = "crash"
            elif tag == "ActivityManager" and ANR.match(message):
                kind = "anr"
        if kind:
            block.append(message)
            last_key = key
    close()
    return crashes


def _crash(kind, block):
    package, pid, thread, exception = "", 0, "", ""
    for message in block:
        fatal, process, anr = FATAL.match(message), PROCESS.match(message), ANR.match(message)
        if fatal:
            thread = fatal.group(1)
        elif process:
            package, pid = process.group(1), int(process.group(2))
        elif anr:
            package, thread = anr.group(1), anr.group(2) or ""
        elif not exception and EXCEPTION.match(message):
            exception = message
    if not package:
        return None
    return Crash(kind, package, pid, thread, exception, tuple(block))


def crashes_of(adb, package, since=None):
    return parse_crashes(read_logcat(adb, since), valid_package(package))

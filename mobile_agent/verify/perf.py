"""What a check measures: launch time, the app's memory, and hangs (docs/testing.mdx, "What a check measures").

Everything here is cheap enough to run on every check: one `ps` every 500 ms, the accessibility reads the run already
makes, and the app's own log. A number Mobster could not measure is None and is named in ``notMeasured``; a budget on
a number that is None never passes (see ``judge``)."""

import math
import os
import re
import subprocess
import threading
from dataclasses import dataclass

SAMPLE_EVERY = .5
HANG_MS = 250
BUDGET_KEYS = ("launch_ms", "memory_mb", "hangs")
# The app's runloop monitor: "Hang detected: 0.52s (debugger attached, not reporting)".
HANG_LINE = re.compile(r"Hang detected: (?P<seconds>\d+(?:\.\d+)?)\s*s\b")
PS = "/bin/ps"
NOT_MEASURED = {
    "firstFrame": "A simulator logs no line for the app's first frame, so only the first stable screen is timed.",
    "droppedFrames": "A simulator exposes no Core Animation commit times, so dropped frames are not counted.",
    "iphoneMemory": ("On an iPhone, memory needs `pymobiledevice3 developer dvt sysmon`, which Mobster does not "
                     "call yet."),
}


def parse_ps(text):
    """(rss_mb, cpu_percent) from `ps -o rss=,pcpu=` for one process; None when the line is not that."""
    parts = (text or "").split()
    if len(parts) != 2:
        return None
    try:
        rss_kb, cpu = int(parts[0]), float(parts[1])
    except ValueError:
        return None
    if rss_kb <= 0 or cpu < 0 or not math.isfinite(cpu):
        return None
    return rss_kb / 1024, cpu


def ps_sample(pid, *, run=subprocess.run):
    """One reading of a simulator app's process (a simulator's processes are the Mac's own); None once it has gone."""
    try:
        done = run([PS, "-o", "rss=,pcpu=", "-p", str(int(pid))], capture_output=True, text=True, timeout=2,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return parse_ps(done.stdout) if done.returncode == 0 else None


def percentile(values, share):
    """The nearest-rank percentile of ``values`` (0 < share <= 1)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def memory_summary(samples):
    """{peakMb, p95Mb, cpuPeakPct, samples} over (rss_mb, cpu) samples; None when there are none."""
    if not samples:
        return None
    memory = [mb for mb, _ in samples]
    return {"peakMb": round(max(memory), 1), "p95Mb": round(percentile(memory, .95), 1),
            "cpuPeakPct": round(max(cpu for _, cpu in samples), 1), "samples": len(samples)}


class Sampler:
    """Reads the app's process when it starts, every SAMPLE_EVERY seconds on a thread of its own while the steps run,
    and once more when it stops: a launch-only check that settles in a second still has its readings (26 Oct 2026,
    iOS 26.4 simulator: runs of about 1.3 s got 2, 2, 0 and 1 samples with the start and the thread alone)."""

    def __init__(self, pid_of, *, sample=None, every=SAMPLE_EVERY):
        self._pid_of, self._sample, self._every = pid_of, sample or ps_sample, every
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """The first reading is taken here, so a run that ends at once still has one."""
        self._take()
        self._thread = threading.Thread(target=self._run, name="mobster-perf-sampler", daemon=True)
        self._thread.start()

    def _take(self):
        try:
            pid = self._pid_of()
            reading = self._sample(pid) if pid else None
        except Exception:  # a sample never costs a run
            reading = None
        if reading is not None:
            self.samples.append(reading)

    def _run(self):
        while not self._stop.wait(self._every):
            self._take()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        self._take()
        return memory_summary(list(self.samples))


def hang_summary(texts, *, threshold_ms=HANG_MS):
    """{count, longestMs, thresholdMs} over the app's log lines: the hang reports of ``threshold_ms`` or longer."""
    found = []
    for text in texts:
        match = HANG_LINE.search(text or "")
        if match and float(match.group("seconds")) * 1000 >= threshold_ms:
            found.append(round(float(match.group("seconds")) * 1000))
    return {"count": len(found), "longestMs": max(found, default=0), "thresholdMs": threshold_ms}


def build(*, launch, sampler_summary, hangs, simulator):
    """The result's ``perf``: the four numbers, the Mac's 1-minute load when they were taken, and what isn't
    measured."""
    missing = {"firstFrame": NOT_MEASURED["firstFrame"]} if simulator else {}
    if simulator:
        missing["droppedFrames"] = NOT_MEASURED["droppedFrames"]
    else:
        missing["droppedFrames"] = "Dropped frames are not measured on an iPhone."
        missing["iphoneMemory"] = NOT_MEASURED["iphoneMemory"]
    try:
        load = round(os.getloadavg()[0], 1)
    except (OSError, AttributeError):
        load = None
    return {"launch": launch, "memory": sampler_summary, "hangs": hangs, "loadAverage": load,
            "notMeasured": missing}


@dataclass(frozen=True)
class Breach:
    key: str
    message: str


def judge(budget, perf):
    """(breaches, unmeasured): which of a check's ``budget`` limits the run broke, and which it could not judge.

    A limit on a number that was not measured is unmeasured, never met: a run that cannot show it stayed inside the
    budget does not pass it."""
    perf = perf or {}
    values = {
        "launch_ms": ((perf.get("launch") or {}).get("interactiveMs"), "Cold launch to the first stable screen",
                      "{:,.0f} ms", "launch time"),
        "memory_mb": ((perf.get("memory") or {}).get("peakMb"), "Peak memory", "{:,.0f} MB", "memory"),
        "hangs": (None if perf.get("hangs") is None else perf["hangs"]["count"], "Hangs of "
                  f"{HANG_MS} ms or longer", "{:,.0f}", "hangs"),
    }
    breaches, unmeasured = [], []
    for key in BUDGET_KEYS:
        limit = (budget or {}).get(key)
        if limit is None:
            continue
        value, what, form, short = values[key]
        if value is None:
            verb = "were" if short == "hangs" else "was"
            unmeasured.append(Breach(key, f"{short.capitalize()} {verb} not measured, so budget.{key} could not be "
                                          "checked."))
        elif value > limit:
            breaches.append(Breach(key, f"{what} was {form.format(value)}, over budget.{key} of {form.format(limit)}."))
    return breaches, unmeasured


# What history keeps of a run's perf, and the unit each is shown in.
METRICS = {"launchMs": "ms", "memoryMb": "MB", "hangs": ""}
REGRESS_AT, BASELINE_RUNS, BASELINE_MIN = .20, 10, 3


def clean(found):
    """``found`` ({launchMs, memoryMb, hangs}) with only the finite, non-negative numbers; the boundary for history."""
    found = found if isinstance(found, dict) else {}
    return {key: found[key] for key in METRICS
            if isinstance(found.get(key), (int, float)) and not isinstance(found[key], bool)
            and math.isfinite(found[key]) and found[key] >= 0}


def numbers(perf):
    """{launchMs, memoryMb, hangs} of a result's ``perf``: only the ones that were measured."""
    perf = perf if isinstance(perf, dict) else {}
    return clean({"launchMs": (perf.get("launch") or {}).get("interactiveMs"),
                  "memoryMb": (perf.get("memory") or {}).get("peakMb"),
                  "hangs": (perf.get("hangs") or {}).get("count")})


def compare(metric, latest, before):
    """(arrow, change) of ``latest`` against the median of ``before`` (a list of earlier passes): ↑ when it is 20 %
    more (a hang count: any more), ↓ when 20 % less, else →; None with fewer than BASELINE_MIN earlier values."""
    before = [value for value in before if value is not None]
    if latest is None or len(before) < BASELINE_MIN:
        return None
    ordered = sorted(before)
    middle = len(ordered) // 2
    base = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    if metric == "hangs":
        arrow = "↑" if latest > base else "↓" if latest < base else "→"
    elif base <= 0:
        return None
    else:
        change = (latest - base) / base
        arrow = "↑" if change > REGRESS_AT else "↓" if change < -REGRESS_AT else "→"
    return arrow, {"latest": latest, "baseline": round(base, 1), "runs": len(before)}


def trend(runs):
    """{metric: {arrow, latest, baseline, runs}} for the newest of ``runs`` (history's, oldest first) against the
    median of the last BASELINE_RUNS passes before it. ↑ is a regression: more time, memory or hangs."""
    if not runs:
        return {}
    *earlier, latest = runs
    passes = [run for run in earlier if run["status"] == "passed"][-BASELINE_RUNS:]
    out = {}
    for metric in METRICS:
        found = compare(metric, (latest.get("perf") or {}).get(metric),
                        [(run.get("perf") or {}).get(metric) for run in passes])
        if found is not None:
            out[metric] = {"arrow": found[0], **found[1]}
    return out


def line(found, trends=None):
    """One line for a report: "launch 812 ms ↑, memory 214 MB →, hangs 0"; "" when nothing was measured."""
    values, trends = clean(found), trends or {}
    parts = []
    for metric, word in (("launchMs", "launch"), ("memoryMb", "memory"), ("hangs", "hangs")):
        if metric in values:
            value = values[metric]
            unit = METRICS[metric]
            text = f"{word} {value:,.0f}" + (f" {unit}" if unit else "")
            arrow = (trends.get(metric) or {}).get("arrow")
            parts.append(text + (f" {arrow}" if arrow else ""))
    return ", ".join(parts)


def last_run(row):
    """`mobster test --history`'s second line for a check, its last run's numbers and markers, or nothing."""
    measured = line(row.get("perfLatest") or {}, row.get("perf"))
    return f"\n    Last run: {measured}." if measured else ""

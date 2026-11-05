"""How much the Mac can take: whether another simulator may boot, and when to wait instead of starting heavy work."""

import os
import re
import subprocess
import time
from dataclasses import dataclass

from .models import SimError

GB = 1 << 30
SIM_GB = 4                 # free memory one more simulator needs
MAX_SIMS_CEILING = 4       # simulators per device type and runtime when MOBSTER_MAX_SIMS is unset
LOAD_WAIT = 30.0           # how long a boot or a WebDriverAgent build waits for the Mac to settle
LOAD_POLL = 2.0


@dataclass(frozen=True)
class Pressure:
    load: float            # the 1-minute load average
    cores: int
    free_gb: float         # memory that can be handed out now: free, inactive and speculative pages


def read_pressure(run=subprocess.run):
    cores = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    free = None
    try:
        out = run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        size = int(re.search(r"page size of (\d+) bytes", out).group(1))
        pages = sum(int(re.search(rf"Pages {name}:\s+(\d+)", out).group(1))
                    for name in ("free", "inactive", "speculative"))
        free = pages * size / GB
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        pass
    # memory that can't be read counts as plenty: the load check and the simulator ceiling still apply
    return Pressure(load, cores, free if free is not None else float(MAX_SIMS_CEILING * SIM_GB))


def load_limit(environ=None):
    """The 1-minute load at which the Mac counts as saturated: one runnable task per core, or $MOBSTER_MAX_LOAD."""
    environ = os.environ if environ is None else environ
    text = str(environ.get("MOBSTER_MAX_LOAD") or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise SimError("environment", f"MOBSTER_MAX_LOAD is {text!r}; it must be a load average above 0.",
                       "Unset it to treat the Mac as saturated at one runnable task per core.")
    return value


class Governor:
    """Reads the Mac's load and memory (``sample``) and decides. Tests pass a ``sample`` that returns a Pressure."""

    def __init__(self, sample=read_pressure, sleep=time.sleep, clock=time.monotonic):
        self.sample, self.sleep, self.clock = sample, sleep, clock

    def saturated(self, pressure=None):
        pressure = pressure or self.sample()
        return pressure.load >= (load_limit() or pressure.cores)

    def memory_room(self, running, pressure=None, starting=0):
        """Whether another simulator may boot while ``running`` are up: one needs 4 GB free; the first always may.
        ``starting`` of them are still booting and haven't taken their memory yet, so SIM_GB is set aside for each."""
        return running < 1 or (pressure or self.sample()).free_gb - SIM_GB * starting >= SIM_GB

    def wait_for_calm(self, say, what, *, timeout=LOAD_WAIT):
        """Return once the Mac isn't saturated. After ``timeout`` seconds, refuse: never start anyway."""
        pressure = self.sample()
        if not self.saturated(pressure):
            return 0.0
        began = self.clock()
        say(f"Waiting to {what}: the Mac is busy (load {pressure.load:.0f} on {pressure.cores} cores)")
        while self.clock() - began < timeout:
            self.sleep(LOAD_POLL)
            pressure = self.sample()
            if not self.saturated(pressure):
                return self.clock() - began
        raise SimError("busy", f"The Mac is busy (load {pressure.load:.0f} on {pressure.cores} cores), so Mobster "
                               f"didn't {what}. Nothing was started.",
                       "Try again in a minute, or stop other heavy jobs. Set MOBSTER_MAX_LOAD to change when the Mac "
                       "counts as busy.")
